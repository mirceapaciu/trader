from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.core_components.event_ingestion_engine.interfaces import StorageAdapter
from src.core_components.event_ingestion_engine.models import (
    CanonicalEvent,
    Checkpoint,
    PublicationObligation,
    PublicationStatus,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresNewsStorageAdapter(StorageAdapter):
    """PostgreSQL implementation of NewsFetcher persistence and outbox contract."""

    def __init__(
        self,
        *,
        dsn: str,
        news_schema: str,
        shared_schema: str,
        watchlist_table: str,
    ) -> None:
        self._dsn = dsn
        self._news_schema = _safe_identifier(news_schema)
        self._shared_schema = _safe_identifier(shared_schema)
        self._watchlist_table = _safe_identifier(watchlist_table)

    def get_checkpoint(self, source_key: str) -> Checkpoint | None:
        sql = (
            f"SELECT source_key, cursor_value, cursor_updated_at, version "
            f"FROM {self._news_schema}.t_source_checkpoints WHERE source_key = %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (source_key,))
            row = cur.fetchone()
        if row is None:
            return None
        return Checkpoint(
            source_key=row["source_key"],
            cursor_value=row["cursor_value"],
            cursor_updated_at=_to_utc(row["cursor_updated_at"]),
            version=int(row["version"]),
        )

    def list_soft_dedupe_candidates(
        self,
        *,
        source: str,
        occurred_at: datetime,
        lookback_window: timedelta,
    ) -> list[CanonicalEvent]:
        lower_bound = _to_utc(occurred_at) - lookback_window
        sql = (
            f"SELECT id, source, headline, summary, url, tickers, published_at, fetched_at, sentiment_source "
            f"FROM {self._news_schema}.t_news_articles "
            f"WHERE source = %s AND published_at BETWEEN %s AND %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (source, lower_bound, _to_utc(occurred_at)))
            rows = cur.fetchall()

        candidates: list[CanonicalEvent] = []
        for row in rows:
            tickers = row.get("tickers") or []
            sentiment = row.get("sentiment_source")
            candidates.append(
                CanonicalEvent(
                    id=row["id"],
                    source=row["source"],
                    source_event_id=row["id"],
                    canonical_locator=row["url"],
                    title=row["headline"],
                    summary=row["summary"],
                    occurred_at=_to_utc(row["published_at"]),
                    ingested_at=_to_utc(row["fetched_at"]),
                    entities=list(tickers),
                    attributes={
                        "tickers": list(tickers),
                        "sentiment_source": sentiment,
                    },
                )
            )
        return candidates

    def persist_batch(
        self,
        *,
        source_key: str,
        batch_id: str,
        accepted_events,
        obligations,
    ) -> None:
        article_sql = (
            f"INSERT INTO {self._news_schema}.t_news_articles "
            f"(id, source, headline, summary, url, tickers, published_at, fetched_at, sentiment_source) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        obligation_sql = (
            f"INSERT INTO {self._news_schema}.t_publication_obligations "
            f"(obligation_id, source_key, batch_id, canonical_event_id, event_type, dedupe_key, envelope_json, status, attempt_count, last_error_code, claimed_by, claim_expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (canonical_event_id, event_type, dedupe_key) DO NOTHING"
        )

        with self._connect() as conn, conn.cursor() as cur:
            for event in accepted_events:
                attributes = event.attributes or {}
                tickers = list(event.entities or attributes.get("tickers") or [])
                sentiment = attributes.get("sentiment_source")
                fetched_at = attributes.get("fetched_at")
                parsed_fetched_at = (
                    _to_utc(datetime.fromisoformat(fetched_at))
                    if isinstance(fetched_at, str)
                    else event.ingested_at
                )
                cur.execute(
                    article_sql,
                    (
                        event.id,
                        event.source,
                        event.title,
                        event.summary,
                        event.canonical_locator,
                        Json(tickers),
                        _to_utc(event.occurred_at),
                        _to_utc(parsed_fetched_at),
                        sentiment,
                    ),
                )

            for obligation in obligations:
                cur.execute(
                    obligation_sql,
                    (
                        obligation.obligation_id,
                        source_key,
                        batch_id,
                        obligation.canonical_event_id,
                        obligation.event_type,
                        obligation.dedupe_key,
                        Json(obligation.envelope_json),
                        obligation.status.value,
                        obligation.attempt_count,
                        obligation.last_error_code,
                        obligation.claimed_by,
                        obligation.claim_expires_at,
                    ),
                )
            conn.commit()

    def load_batch_obligations(self, *, batch_id: str) -> list[PublicationObligation]:
        sql = (
            f"SELECT obligation_id, source_key, batch_id, canonical_event_id, event_type, dedupe_key, envelope_json, "
            f"status, attempt_count, last_error_code, claimed_by, claim_expires_at "
            f"FROM {self._news_schema}.t_publication_obligations WHERE batch_id = %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (batch_id,))
            rows = cur.fetchall()

        obligations: list[PublicationObligation] = []
        for row in rows:
            obligations.append(
                PublicationObligation(
                    obligation_id=row["obligation_id"],
                    source_key=row["source_key"],
                    batch_id=row["batch_id"],
                    canonical_event_id=row["canonical_event_id"],
                    event_type=row["event_type"],
                    dedupe_key=row["dedupe_key"],
                    envelope_json=row["envelope_json"],
                    status=PublicationStatus(row["status"]),
                    attempt_count=row["attempt_count"],
                    last_error_code=row["last_error_code"],
                    claimed_by=row["claimed_by"],
                    claim_expires_at=row["claim_expires_at"],
                )
            )
        return obligations

    def mark_obligation_status(
        self,
        *,
        obligation_id: str,
        status: PublicationStatus,
        last_error_code: str | None,
    ) -> None:
        sql = (
            f"UPDATE {self._news_schema}.t_publication_obligations "
            f"SET status = %s, last_error_code = %s, updated_at = NOW(), "
            f"claimed_by = CASE WHEN %s IN ('published', 'dead_lettered') THEN NULL ELSE claimed_by END, "
            f"claim_expires_at = CASE WHEN %s IN ('published', 'dead_lettered') THEN NULL ELSE claim_expires_at END "
            f"WHERE obligation_id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (status.value, last_error_code, status.value, status.value, obligation_id))
            conn.commit()

    def has_non_terminal_obligations(self, *, batch_id: str) -> bool:
        sql = (
            f"SELECT 1 FROM {self._news_schema}.t_publication_obligations "
            f"WHERE batch_id = %s AND status IN ('pending', 'publishing') LIMIT 1"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (batch_id,))
            return cur.fetchone() is not None

    def advance_checkpoint(
        self,
        *,
        source_key: str,
        expected_version: int,
        new_cursor: Any,
        cursor_updated_at: datetime,
    ) -> bool:
        update_sql = (
            f"UPDATE {self._news_schema}.t_source_checkpoints "
            f"SET cursor_value = %s, cursor_updated_at = %s, version = version + 1, updated_at = NOW() "
            f"WHERE source_key = %s AND version = %s"
        )
        insert_sql = (
            f"INSERT INTO {self._news_schema}.t_source_checkpoints "
            f"(source_key, cursor_value, cursor_updated_at, version, updated_at) "
            f"VALUES (%s, %s, %s, 1, NOW())"
        )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                update_sql,
                (
                    Json(new_cursor),
                    _to_utc(cursor_updated_at),
                    source_key,
                    expected_version,
                ),
            )
            if cur.rowcount == 1:
                conn.commit()
                return True

            if expected_version != 0:
                conn.rollback()
                return False

            try:
                cur.execute(
                    insert_sql,
                    (
                        source_key,
                        Json(new_cursor),
                        _to_utc(cursor_updated_at),
                    ),
                )
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                return False

            conn.commit()
            return cur.rowcount == 1

    def load_active_watchlist_tickers(self) -> set[str]:
        sql = (
            f"SELECT ticker FROM {self._shared_schema}.{self._watchlist_table} "
            f"WHERE is_active = TRUE"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return {str(row[0]).strip().upper() for row in rows if str(row[0]).strip()}

    def record_provider_cycle_status(
        self,
        *,
        source_key: str,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        error_code: str | None,
        fetched_count: int,
        accepted_count: int,
        rejected_count: int,
        checkpoint_advanced: bool,
    ) -> None:
        sql = (
            f"INSERT INTO {self._news_schema}.t_provider_cycle_status "
            f"(source_key, last_cycle_started_at, last_cycle_finished_at, last_cycle_status, "
            f"last_cycle_error_code, last_cycle_fetched_count, last_cycle_accepted_count, "
            f"last_cycle_rejected_count, last_cycle_checkpoint_advanced, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
            f"ON CONFLICT (source_key) DO UPDATE SET "
            f"last_cycle_started_at = EXCLUDED.last_cycle_started_at, "
            f"last_cycle_finished_at = EXCLUDED.last_cycle_finished_at, "
            f"last_cycle_status = EXCLUDED.last_cycle_status, "
            f"last_cycle_error_code = EXCLUDED.last_cycle_error_code, "
            f"last_cycle_fetched_count = EXCLUDED.last_cycle_fetched_count, "
            f"last_cycle_accepted_count = EXCLUDED.last_cycle_accepted_count, "
            f"last_cycle_rejected_count = EXCLUDED.last_cycle_rejected_count, "
            f"last_cycle_checkpoint_advanced = EXCLUDED.last_cycle_checkpoint_advanced, "
            f"updated_at = NOW()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    source_key,
                    _to_utc(started_at),
                    _to_utc(finished_at),
                    status,
                    error_code,
                    fetched_count,
                    accepted_count,
                    rejected_count,
                    checkpoint_advanced,
                ),
            )
            conn.commit()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
