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
from src.product_components.news_fetcher.rss_feeds import (
    RssFeedSpec,
    RssTickerMatch,
    WatchlistTicker,
    build_rss_feed_url,
    rss_batch_source_key,
    rss_source_key,
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
        return {row.ticker for row in self.load_active_watchlist_rows()}

    def load_active_watchlist_rows(self) -> list[WatchlistTicker]:
        sql = (
            f"SELECT ticker, exchange_code FROM {self._shared_schema}.{self._watchlist_table} "
            f"WHERE is_active = TRUE"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        watchlist: list[WatchlistTicker] = []
        for row in rows:
            ticker = str(row[0]).strip().upper()
            exchange_code = str(row[1]).strip().upper()
            if ticker and exchange_code:
                watchlist.append(WatchlistTicker(ticker=ticker, exchange_code=exchange_code))
        return watchlist

    def load_rss_feed_specs(self) -> list[RssFeedSpec]:
        specs = self._load_static_rss_feed_specs()
        specs.extend(self._load_dynamic_rss_feed_specs())
        return specs

    def _load_static_rss_feed_specs(self) -> list[RssFeedSpec]:
        sql = (
            f"SELECT source_key, base_url, min_request_interval_seconds "
            f"FROM {self._news_schema}.t_rss_sources "
            f"WHERE is_enabled = TRUE AND source_type = 'static' "
            f"ORDER BY source_key"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        return [
            RssFeedSpec(
                source_key=str(row["source_key"]),
                provider_name=str(row["source_key"]),
                url=str(row["base_url"]),
                app_tickers=(),
                ticker_matches=(),
                min_request_interval_seconds=int(row["min_request_interval_seconds"] or 0),
            )
            for row in rows
        ]

    def _load_dynamic_rss_feed_specs(self) -> list[RssFeedSpec]:
        sql = (
            f"SELECT "
            f"w.ticker, w.exchange_code, s.source_key, s.base_url, s.symbol_param, "
            f"s.default_query_params, s.max_symbols_per_request, s.min_request_interval_seconds, "
            f"s.grouping_mode, r.provider_symbol, r.query_params, "
            f"COALESCE(a.aliases, '[]'::jsonb) AS aliases "
            f"FROM {self._shared_schema}.{self._watchlist_table} w "
            f"CROSS JOIN {self._news_schema}.t_rss_sources s "
            f"LEFT JOIN {self._news_schema}.t_rss_symbol_rules r "
            f"ON r.source_key = s.source_key "
            f"AND UPPER(r.ticker) = UPPER(w.ticker) "
            f"AND UPPER(r.exchange_code) = UPPER(w.exchange_code) "
            f"AND r.is_enabled = TRUE "
            f"LEFT JOIN ("
            f"SELECT ticker, exchange_code, jsonb_agg(alias ORDER BY alias) AS aliases "
            f"FROM {self._shared_schema}.t_instrument_aliases "
            f"GROUP BY ticker, exchange_code"
            f") a ON UPPER(a.ticker) = UPPER(w.ticker) "
            f"AND UPPER(a.exchange_code) = UPPER(w.exchange_code) "
            f"WHERE w.is_active = TRUE AND s.is_enabled = TRUE "
            f"AND s.source_type = 'dynamic_watchlist' "
            f"ORDER BY s.source_key, w.ticker, w.exchange_code"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        grouped_rows: dict[tuple[str, str, str, tuple[tuple[str, str], ...], int, int, str], list[dict[str, Any]]] = {}
        for row in rows:
            ticker = str(row["ticker"]).strip().upper()
            exchange_code = str(row["exchange_code"]).strip().upper()
            if not ticker or not exchange_code:
                continue

            default_params = _string_dict(row["default_query_params"])
            override_params = _string_dict(row["query_params"])
            query_params = {**default_params, **override_params}
            key = (
                str(row["source_key"]).strip(),
                str(row["base_url"]),
                str(row["symbol_param"]),
                tuple(sorted(query_params.items())),
                int(row["max_symbols_per_request"] or 10),
                int(row["min_request_interval_seconds"] or 0),
                str(row["grouping_mode"] or "grouped"),
            )
            enriched = dict(row)
            enriched["_ticker"] = ticker
            enriched["_exchange_code"] = exchange_code
            enriched["_provider_symbol"] = str(row["provider_symbol"] or ticker).strip().upper()
            enriched["_query_params"] = query_params
            grouped_rows.setdefault(key, []).append(enriched)

        specs: list[RssFeedSpec] = []
        for (
            source_name,
            base_url,
            symbol_param,
            query_param_items,
            max_symbols,
            min_interval,
            grouping_mode,
        ), source_rows in grouped_rows.items():
            query_params = dict(query_param_items)
            if grouping_mode == "single":
                specs.extend(
                    _build_single_rss_specs(
                        source_name=source_name,
                        base_url=base_url,
                        symbol_param=symbol_param,
                        query_params=query_params,
                        min_interval=min_interval,
                        rows=source_rows,
                    )
                )
                continue

            for chunk in _chunks(source_rows, max_symbols):
                provider_symbols = tuple(row["_provider_symbol"] for row in chunk)
                specs.append(
                    RssFeedSpec(
                        source_key=rss_batch_source_key(
                            provider_name=source_name,
                            provider_symbols=provider_symbols,
                        ),
                        provider_name=source_name,
                        url=build_rss_feed_url(
                            base_url=base_url,
                            symbol_param=symbol_param,
                            provider_symbol=provider_symbols,
                            query_params=query_params,
                        ),
                        app_tickers=tuple(row["_ticker"] for row in chunk),
                        ticker_matches=tuple(_ticker_match(row) for row in chunk),
                        min_request_interval_seconds=min_interval,
                    )
                )
        return specs

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


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = str(key).strip()
        normalized_value = str(item).strip()
        if normalized_key and normalized_value:
            result[normalized_key] = normalized_value
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _ticker_match(row: dict[str, Any]) -> RssTickerMatch:
    terms = {
        str(row["_ticker"]).strip(),
        str(row["_provider_symbol"]).strip(),
        *(_string_list(row.get("aliases"))),
    }
    normalized = tuple(sorted({term for term in terms if term}))
    return RssTickerMatch(ticker=str(row["_ticker"]), match_terms=normalized)


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    safe_size = max(1, size)
    return [rows[index : index + safe_size] for index in range(0, len(rows), safe_size)]


def _build_single_rss_specs(
    *,
    source_name: str,
    base_url: str,
    symbol_param: str,
    query_params: dict[str, str],
    min_interval: int,
    rows: list[dict[str, Any]],
) -> list[RssFeedSpec]:
    specs: list[RssFeedSpec] = []
    for row in rows:
        specs.append(
            RssFeedSpec(
                source_key=rss_source_key(
                    provider_name=source_name,
                    ticker=str(row["_ticker"]),
                    exchange_code=str(row["_exchange_code"]),
                ),
                provider_name=source_name,
                url=build_rss_feed_url(
                    base_url=base_url,
                    symbol_param=symbol_param,
                    provider_symbol=str(row["_provider_symbol"]),
                    query_params=query_params,
                ),
                app_tickers=(str(row["_ticker"]),),
                ticker_matches=(_ticker_match(row),),
                min_request_interval_seconds=min_interval,
            )
        )
    return specs
