from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SharedInstrumentRecord:
    ticker: str
    exchange_code: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class SharedInstrumentSeed:
    ticker: str
    exchange_code: str
    display_name: str
    aliases: tuple[str, ...]
    identifiers: dict[str, Any]
    enabled: bool


@dataclass(frozen=True)
class SharedWatchlistRecord:
    ticker: str
    exchange_code: str
    display_name: str | None
    aliases: tuple[str, ...]
    is_active: bool
    source: str

    @property
    def has_missing_aliases(self) -> bool:
        return len(self.aliases) == 0


@dataclass(frozen=True)
class SharedWatchlistEntryInput:
    ticker: str
    exchange_code: str
    display_name: str
    aliases: tuple[str, ...] = ()
    identifiers: dict[str, Any] = field(default_factory=dict)
    source: str = "manual"
    enabled: bool = True


@dataclass(frozen=True)
class SharedLookupCacheEntry:
    operation: str
    target: str
    provider: str
    payload: dict[str, Any]
    fetched_at: datetime
    expires_at: datetime


class PostgresSharedInstrumentRegistry:
    def __init__(
        self,
        *,
        dsn: str,
        shared_schema: str,
        watchlist_table: str,
    ) -> None:
        self._dsn = dsn
        self._shared_schema = _safe_identifier(shared_schema)
        self._watchlist_table = _safe_identifier(watchlist_table)

    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        return [
            SharedInstrumentRecord(
                ticker=row.ticker,
                exchange_code=row.exchange_code,
                aliases=row.aliases,
            )
            for row in self.list_watchlist_records(active_only=True)
        ]

    def list_watchlist_records(self, *, active_only: bool = True) -> list[SharedWatchlistRecord]:
        where_clause = "WHERE w.is_active = TRUE " if active_only else ""
        sql = (
            f"SELECT w.ticker, w.exchange_code, w.is_active, w.source, i.display_name, "
            f"COALESCE(jsonb_agg(DISTINCT a.alias) FILTER (WHERE a.alias IS NOT NULL), '[]'::jsonb) AS aliases "
            f"FROM {self._shared_schema}.{self._watchlist_table} w "
            f"LEFT JOIN {self._shared_schema}.t_instruments i "
            f"ON UPPER(i.ticker) = UPPER(w.ticker) AND UPPER(i.exchange_code) = UPPER(w.exchange_code) "
            f"LEFT JOIN {self._shared_schema}.t_instrument_aliases a "
            f"ON UPPER(a.ticker) = UPPER(w.ticker) AND UPPER(a.exchange_code) = UPPER(w.exchange_code) "
            f"{where_clause}"
            f"GROUP BY w.ticker, w.exchange_code, w.is_active, w.source, i.display_name "
            f"ORDER BY w.ticker, w.exchange_code"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [
            SharedWatchlistRecord(
                ticker=str(row["ticker"]).strip().upper(),
                exchange_code=str(row["exchange_code"]).strip().upper(),
                display_name=str(row.get("display_name")).strip() if row.get("display_name") else None,
                aliases=tuple(
                    str(alias).strip().lower()
                    for alias in (row["aliases"] or [])
                    if str(alias).strip()
                ),
                is_active=bool(row.get("is_active", True)),
                source=str(row.get("source")).strip() if row.get("source") else "manual",
            )
            for row in rows
        ]

    def get_watchlist_record(self, *, ticker: str, exchange_code: str) -> SharedWatchlistRecord | None:
        sql = (
            f"SELECT w.ticker, w.exchange_code, w.is_active, w.source, i.display_name, "
            f"COALESCE(jsonb_agg(DISTINCT a.alias) FILTER (WHERE a.alias IS NOT NULL), '[]'::jsonb) AS aliases "
            f"FROM {self._shared_schema}.{self._watchlist_table} w "
            f"LEFT JOIN {self._shared_schema}.t_instruments i "
            f"ON UPPER(i.ticker) = UPPER(w.ticker) AND UPPER(i.exchange_code) = UPPER(w.exchange_code) "
            f"LEFT JOIN {self._shared_schema}.t_instrument_aliases a "
            f"ON UPPER(a.ticker) = UPPER(w.ticker) AND UPPER(a.exchange_code) = UPPER(w.exchange_code) "
            f"WHERE UPPER(w.ticker) = UPPER(%s) AND UPPER(w.exchange_code) = UPPER(%s) "
            f"GROUP BY w.ticker, w.exchange_code, w.is_active, w.source, i.display_name"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (ticker.strip().upper(), exchange_code.strip().upper()))
            row = cur.fetchone()
        if row is None:
            return None
        return SharedWatchlistRecord(
            ticker=str(row["ticker"]).strip().upper(),
            exchange_code=str(row["exchange_code"]).strip().upper(),
            display_name=str(row["display_name"]).strip() if row["display_name"] else None,
            aliases=tuple(
                str(alias).strip().lower()
                for alias in (row["aliases"] or [])
                if str(alias).strip()
            ),
            is_active=bool(row["is_active"]),
            source=str(row["source"]).strip() if row["source"] else "manual",
        )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


class PostgresSharedInstrumentAdmin:
    def __init__(
        self,
        *,
        dsn: str,
        shared_schema: str,
    ) -> None:
        self._dsn = dsn
        self._shared_schema = _safe_identifier(shared_schema)

    def upsert_instruments(self, instruments: tuple[SharedInstrumentSeed, ...]) -> None:
        if not instruments:
            return
        with self._connect() as conn, conn.cursor() as cur:
            for instrument in instruments:
                self._upsert_instrument(cur, instrument)
            conn.commit()

    def upsert_watchlist_entry(self, entry: SharedWatchlistEntryInput, *, replace_aliases: bool = True) -> None:
        normalized = SharedInstrumentSeed(
            ticker=entry.ticker.strip().upper(),
            exchange_code=entry.exchange_code.strip().upper(),
            display_name=entry.display_name.strip() or entry.ticker.strip().upper(),
            aliases=tuple(alias.strip() for alias in entry.aliases if alias.strip()),
            identifiers=dict(entry.identifiers),
            enabled=entry.enabled,
        )
        with self._connect() as conn, conn.cursor() as cur:
            self._upsert_instrument(cur, normalized)
            if replace_aliases:
                cur.execute(
                    f"""
                    DELETE FROM {self._shared_schema}.t_instrument_aliases
                    WHERE UPPER(ticker) = UPPER(%s)
                      AND UPPER(exchange_code) = UPPER(%s)
                      AND alias_type <> 'identifier'
                    """,
                    (normalized.ticker, normalized.exchange_code),
                )
                self._insert_aliases(cur, normalized)
            cur.execute(
                f"""
                INSERT INTO {self._shared_schema}.t_watchlist_tickers
                    (ticker, exchange_code, is_active, source, created_at, updated_at)
                VALUES (%s, %s, TRUE, %s, NOW(), NOW())
                ON CONFLICT (ticker, exchange_code)
                DO UPDATE SET
                    is_active = TRUE,
                    source = EXCLUDED.source,
                    updated_at = NOW()
                """,
                (normalized.ticker, normalized.exchange_code, entry.source.strip() or "manual"),
            )
            conn.commit()

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self._shared_schema}.t_watchlist_tickers
                SET is_active = FALSE, updated_at = NOW()
                WHERE UPPER(ticker) = UPPER(%s) AND UPPER(exchange_code) = UPPER(%s)
                """,
                (ticker.strip().upper(), exchange_code.strip().upper()),
            )
            conn.commit()

    def load_lookup_cache(self, *, operation: str, target: str) -> SharedLookupCacheEntry | None:
        sql = (
            f"SELECT operation, target, provider, payload_json, fetched_at, expires_at "
            f"FROM {self._shared_schema}.t_instrument_lookup_cache "
            f"WHERE operation = %s AND target = %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (operation, target))
            row = cur.fetchone()
        if row is None:
            return None
        return SharedLookupCacheEntry(
            operation=str(row["operation"]),
            target=str(row["target"]),
            provider=str(row["provider"]),
            payload=dict(row["payload_json"] or {}),
            fetched_at=_to_utc(row["fetched_at"]),
            expires_at=_to_utc(row["expires_at"]),
        )

    def save_lookup_cache(
        self,
        *,
        operation: str,
        target: str,
        provider: str,
        payload: dict[str, Any],
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self._shared_schema}.t_instrument_lookup_cache
                    (operation, target, provider, payload_json, fetched_at, expires_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (operation, target)
                DO UPDATE SET
                    provider = EXCLUDED.provider,
                    payload_json = EXCLUDED.payload_json,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """,
                (operation, target, provider, Json(payload), _to_utc(fetched_at), _to_utc(expires_at)),
            )
            conn.commit()

    def _upsert_instrument(self, cur, instrument: SharedInstrumentSeed) -> None:
        cur.execute(
            f"""
            INSERT INTO {self._shared_schema}.t_instruments
                (ticker, exchange_code, display_name, identifiers, is_enabled, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (ticker, exchange_code)
            DO UPDATE SET
                display_name = EXCLUDED.display_name,
                identifiers = EXCLUDED.identifiers,
                is_enabled = EXCLUDED.is_enabled,
                updated_at = NOW()
            """,
            (
                instrument.ticker,
                instrument.exchange_code,
                instrument.display_name,
                Json(instrument.identifiers),
                instrument.enabled,
            ),
        )
        self._insert_aliases(cur, instrument)

    def _insert_aliases(self, cur, instrument: SharedInstrumentSeed) -> None:
        identifier_values = {str(value) for value in instrument.identifiers.values()}
        aliases = {
            instrument.ticker,
            instrument.display_name,
            *instrument.aliases,
            *identifier_values,
        }
        for alias in sorted({alias.strip() for alias in aliases if alias and alias.strip()}):
            cur.execute(
                f"""
                INSERT INTO {self._shared_schema}.t_instrument_aliases
                    (ticker, exchange_code, alias, alias_type, created_at, updated_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (ticker, exchange_code, alias)
                DO UPDATE SET alias_type = EXCLUDED.alias_type, updated_at = NOW()
                """,
                (
                    instrument.ticker,
                    instrument.exchange_code,
                    alias,
                    "identifier" if alias in identifier_values else "alias",
                ),
            )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


class PostgresSharedApiUsageWriter:
    def __init__(
        self,
        *,
        dsn: str,
        shared_schema: str,
    ) -> None:
        self._dsn = dsn
        self._shared_schema = _safe_identifier(shared_schema)

    def record_usage(
        self,
        *,
        provider: str,
        endpoint: str,
        called_at: datetime,
        tokens_used: int | None = None,
        cost_estimate: float | None = None,
    ) -> None:
        sql = (
            f"INSERT INTO {self._shared_schema}.t_api_usage "
            f"(provider, endpoint, tokens_used, cost_estimate, called_at) "
            f"VALUES (%s, %s, %s, %s, %s)"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (provider, endpoint, tokens_used, cost_estimate, _to_utc(called_at)),
            )
            conn.commit()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


class PostgresSharedThesisCardReviewWriter:
    def __init__(
        self,
        *,
        dsn: str,
        shared_schema: str,
    ) -> None:
        self._dsn = dsn
        self._shared_schema = _safe_identifier(shared_schema)

    def upsert_system_approved_review(
        self,
        *,
        card_id: str,
        reviewed_at: datetime,
        review_reason: str = "thesis_builder_preapproved_v1",
    ) -> None:
        sql = (
            f"INSERT INTO {self._shared_schema}.t_thesis_card_reviews "
            f"(card_id, decision_state, reviewed_by, review_reason, reviewed_at) "
            f"VALUES (%s, 'approved', 'system_policy', %s, %s) "
            f"ON CONFLICT (card_id) DO UPDATE SET "
            f"decision_state = EXCLUDED.decision_state, "
            f"reviewed_by = EXCLUDED.reviewed_by, "
            f"review_reason = EXCLUDED.review_reason, "
            f"reviewed_at = EXCLUDED.reviewed_at"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (card_id, review_reason, _to_utc(reviewed_at)))
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
