from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ExportedEvidenceArticle:
    article_id: str
    published_at: datetime
    fetched_at: datetime


@dataclass(frozen=True)
class ExportedThesisCard:
    id: str
    ticker: str
    exchange_code: str
    direction: str
    strategy: str
    time_horizon: str
    confidence: float
    risk_max_loss_usd: float
    risk_stop_condition: str
    risk_invalidation_condition: str
    validation_status: str
    rejection_reason_code: str | None
    created_at: datetime
    expires_at: datetime
    signal_published_at: datetime | None
    evidence: list[ExportedEvidenceArticle]
    news_ready_at: datetime


class ThesisCardHistoryExporter:
    """Read-only card-history export contract consumed by the Backtester."""

    def __init__(self, *, dsn: str, thesis_schema: str = "thesis_builder") -> None:
        self._dsn = dsn
        self._thesis_schema = _safe_identifier(thesis_schema)

    def export_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        validation_status: str | None = None,
        strategy: str | None = None,
    ) -> list[ExportedThesisCard]:
        params: list[Any] = [_to_utc(window_start_at), _to_utc(window_end_at)]
        filters = ""
        if validation_status is not None:
            filters += "AND validation_status = %s "
            params.append(validation_status)
        if strategy is not None:
            filters += "AND strategy = %s "
            params.append(strategy)
        card_sql = (
            f"SELECT id, ticker, exchange_code, direction, strategy, time_horizon, confidence, "
            f"risk_max_loss_usd, risk_stop_condition, risk_invalidation_condition, evidence, "
            f"validation_status, rejection_reason_code, created_at, expires_at, signal_published_at "
            f"FROM {self._thesis_schema}.t_thesis_cards "
            f"WHERE created_at >= %s AND created_at < %s "
            f"{filters}"
            f"ORDER BY created_at, id"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(card_sql, tuple(params))
            card_rows = cur.fetchall()
            article_ids = _collect_evidence_article_ids(card_rows)
            analysis_rows_by_article_id = self._load_analysis_timing(conn, article_ids)
        return [
            build_exported_card(card_row, analysis_rows_by_article_id) for card_row in card_rows
        ]

    def _load_analysis_timing(
        self,
        conn: psycopg.Connection,
        article_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not article_ids:
            return {}
        sql = (
            f"SELECT article_id, article_snapshot "
            f"FROM {self._thesis_schema}.t_news_analyses "
            f"WHERE article_id = ANY(%s)"
        )
        rows_by_article_id: dict[str, dict[str, Any]] = {}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (article_ids,))
            for row in cur.fetchall():
                article_id = str(row["article_id"])
                rows_by_article_id.setdefault(article_id, dict(row["article_snapshot"] or {}))
        return rows_by_article_id

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def _collect_evidence_article_ids(card_rows: list[dict[str, Any]]) -> list[str]:
    article_ids: list[str] = []
    seen: set[str] = set()
    for card_row in card_rows:
        for entry in card_row.get("evidence") or []:
            article_id = _evidence_article_id(entry)
            if article_id and article_id not in seen:
                seen.add(article_id)
                article_ids.append(article_id)
    return article_ids


def build_exported_card(
    card_row: dict[str, Any],
    analysis_rows_by_article_id: dict[str, dict[str, Any]],
) -> ExportedThesisCard:
    created_at = _to_utc(card_row["created_at"])
    evidence: list[ExportedEvidenceArticle] = []
    for entry in card_row.get("evidence") or []:
        article_id = _evidence_article_id(entry)
        if not article_id:
            continue
        snapshot = analysis_rows_by_article_id.get(article_id)
        if snapshot is None:
            continue
        published_at = _snapshot_timestamp(snapshot, "published_at")
        fetched_at = _snapshot_timestamp(snapshot, "fetched_at")
        if published_at is None or fetched_at is None:
            continue
        evidence.append(
            ExportedEvidenceArticle(
                article_id=article_id,
                published_at=published_at,
                fetched_at=fetched_at,
            )
        )
    published_times = [item.published_at for item in evidence]
    news_ready_at = max(published_times) if published_times else created_at
    return ExportedThesisCard(
        id=str(card_row["id"]),
        ticker=str(card_row["ticker"]),
        exchange_code=str(card_row["exchange_code"]),
        direction=str(card_row["direction"]),
        strategy=str(card_row["strategy"]),
        time_horizon=str(card_row["time_horizon"]),
        confidence=float(card_row["confidence"]),
        risk_max_loss_usd=float(card_row["risk_max_loss_usd"]),
        risk_stop_condition=str(card_row["risk_stop_condition"]),
        risk_invalidation_condition=str(card_row["risk_invalidation_condition"]),
        validation_status=str(card_row["validation_status"]),
        rejection_reason_code=card_row["rejection_reason_code"],
        created_at=created_at,
        expires_at=_to_utc(card_row["expires_at"]),
        signal_published_at=(
            _to_utc(card_row["signal_published_at"])
            if card_row.get("signal_published_at") is not None
            else None
        ),
        evidence=evidence,
        news_ready_at=news_ready_at,
    )


def _evidence_article_id(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    article_id = entry.get("article_id")
    return str(article_id) if article_id is not None else None


def _snapshot_timestamp(snapshot: dict[str, Any], key: str) -> datetime | None:
    value = snapshot.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    return _to_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
