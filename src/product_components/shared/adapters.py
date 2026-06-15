from __future__ import annotations

import re
from dataclasses import dataclass
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
        sql = (
            f"SELECT w.ticker, w.exchange_code, "
            f"COALESCE(jsonb_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL), '[]'::jsonb) AS aliases "
            f"FROM {self._shared_schema}.{self._watchlist_table} w "
            f"LEFT JOIN {self._shared_schema}.t_instrument_aliases a "
            f"ON UPPER(a.ticker) = UPPER(w.ticker) AND UPPER(a.exchange_code) = UPPER(w.exchange_code) "
            f"WHERE w.is_active = TRUE "
            f"GROUP BY w.ticker, w.exchange_code "
            f"ORDER BY w.ticker, w.exchange_code"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [
            SharedInstrumentRecord(
                ticker=str(row["ticker"]).strip().upper(),
                exchange_code=str(row["exchange_code"]).strip().upper(),
                aliases=tuple(
                    str(alias).strip().lower()
                    for alias in (row["aliases"] or [])
                    if str(alias).strip()
                ),
            )
            for row in rows
        ]

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
