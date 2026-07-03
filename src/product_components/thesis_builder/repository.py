from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import (
    ContentType,
    InstrumentIdentity,
    LlmAnalysisResult,
    NewsArticle,
    PersistedAnalysis,
    ThesisCardSignal,
    ThesisStrategy,
    TradeDirection,
    ValidationStatus,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_EXECUTABLE_STRATEGIES = {
    ThesisStrategy.EVENT_DRIVEN,
    ThesisStrategy.SENTIMENT_MOMENTUM,
}


@dataclass(frozen=True)
class AnalysisPersistenceResult:
    analysis_id: int
    signal: ThesisCardSignal | None = None


@dataclass(frozen=True)
class ReprocessRunRecord:
    run_id: str
    days_back: int
    max_articles: int | None
    status: str
    articles_found: int | None
    analyses_created: int | None
    cards_created: int | None
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ReprocessRunAlreadyActive(RuntimeError):
    """Raised when an accepted/running reprocess run already exists."""

    def __init__(self, run_id: str | None = None) -> None:
        super().__init__("reprocess_run_already_active")
        self.run_id = run_id


class PostgresThesisBuilderRepository:
    def __init__(
        self,
        *,
        dsn: str,
        thesis_schema: str,
    ) -> None:
        self._dsn = dsn
        self._thesis_schema = _safe_identifier(thesis_schema)

    def persist_rejected_analysis(
        self,
        *,
        article: NewsArticle,
        instrument: InstrumentIdentity,
        rejection_reason_code: str,
        llm_model: str,
        validation_errors: list[str],
    ) -> int:
        fallback = LlmAnalysisResult(
            ticker=instrument.ticker,
            exchange_code=instrument.exchange_code,
            sentiment=0.0,
            relevance=0.0,
            urgency="informational",
            suggested_action="hold",
            candidate_strategy=ThesisStrategy.EVENT_DRIVEN,
            direction=TradeDirection.HOLD,
            confidence=0.0,
            reasoning=rejection_reason_code,
            is_market_moving=False,
            llm_model=llm_model,
        )
        with self._connect() as conn:
            analysis_id = self._insert_analysis(
                conn=conn,
                article=article,
                result=fallback,
                validation_status=ValidationStatus.REJECTED,
                rejection_reason_code=rejection_reason_code,
                validation_errors=validation_errors,
                market_context_snapshot=None,
            )
            conn.commit()
        return analysis_id

    def persist_analysis_and_update_evidence(
        self,
        *,
        article: NewsArticle,
        result: LlmAnalysisResult,
        market_context_snapshot: dict[str, Any] | None,
        required_evidence_count: int,
        min_confidence: float,
        min_relevance: float = 0.0,
        risk_max_loss_usd: float,
        default_time_horizon: str,
        evidence_collection_max_minutes: int,
        max_evidence_age_minutes: int,
        clock: Callable[[], datetime] | None = None,
        reprocess_run_id: str | None = None,
    ) -> AnalysisPersistenceResult:
        rejection = _analysis_rejection(
            result, min_confidence=min_confidence, min_relevance=min_relevance
        )
        status = ValidationStatus.REJECTED if rejection else ValidationStatus.VALID
        with self._connect() as conn:
            analysis_id = self._insert_analysis(
                conn=conn,
                article=article,
                result=result,
                validation_status=status,
                rejection_reason_code=rejection,
                validation_errors=[rejection] if rejection else [],
                market_context_snapshot=market_context_snapshot,
            )
            signal = None
            if status is ValidationStatus.VALID:
                signal = self._update_window_and_maybe_create_card(
                    conn=conn,
                    analysis_id=analysis_id,
                    article=article,
                    result=result,
                    market_context_snapshot=market_context_snapshot,
                    required_evidence_count=required_evidence_count,
                    risk_max_loss_usd=risk_max_loss_usd,
                    default_time_horizon=default_time_horizon,
                    evidence_collection_max_minutes=evidence_collection_max_minutes,
                    max_evidence_age_minutes=max_evidence_age_minutes,
                    clock=clock,
                    reprocess_run_id=reprocess_run_id,
                )
            conn.commit()
        return AnalysisPersistenceResult(analysis_id=analysis_id, signal=signal)

    def mark_signal_published(self, thesis_card_id: str, *, published_at: datetime) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_thesis_cards "
            f"SET signal_published_at = %s WHERE id = %s AND signal_published_at IS NULL"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (_to_utc(published_at), thesis_card_id))
            conn.commit()

    def record_message_processing_event(
        self,
        *,
        source_message_id: str,
        event_id: str,
        article_id: str,
        outcome: str,
        reason_code: str | None,
        analyses_created: int,
        signals_published: int,
        payload: dict[str, Any],
        processed_at: datetime | None = None,
    ) -> None:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_message_processing_events "
            f"(source_message_id, event_id, article_id, outcome, reason_code, analyses_created, "
            f"signals_published, processed_at, payload_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (source_message_id) DO UPDATE SET "
            f"event_id = EXCLUDED.event_id, "
            f"article_id = EXCLUDED.article_id, "
            f"outcome = EXCLUDED.outcome, "
            f"reason_code = EXCLUDED.reason_code, "
            f"analyses_created = EXCLUDED.analyses_created, "
            f"signals_published = EXCLUDED.signals_published, "
            f"processed_at = EXCLUDED.processed_at, "
            f"payload_json = EXCLUDED.payload_json"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    source_message_id,
                    event_id,
                    article_id,
                    outcome,
                    reason_code,
                    analyses_created,
                    signals_published,
                    _to_utc(processed_at or datetime.now(timezone.utc)),
                    Json(payload),
                ),
            )
            conn.commit()

    def _insert_analysis(
        self,
        *,
        conn: psycopg.Connection,
        article: NewsArticle,
        result: LlmAnalysisResult,
        validation_status: ValidationStatus,
        rejection_reason_code: str | None,
        validation_errors: list[str],
        market_context_snapshot: dict[str, Any] | None,
    ) -> int:
        market_status = _market_context_status(market_context_snapshot)
        market_as_of = _market_context_as_of(market_context_snapshot)
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_news_analyses "
            f"(article_id, ticker, exchange_code, sentiment, relevance, urgency, suggested_action, "
            f"strategy, direction, event_type, price_impact_magnitude, reasoning, confidence, article_snapshot, "
            f"market_context_status, market_context_as_of, market_context_snapshot, is_market_moving, "
            f"content_type, validation_status, validation_errors, rejection_reason_code, llm_model, tokens_used, analyzed_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING id"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    article.id,
                    result.ticker,
                    result.exchange_code,
                    result.sentiment,
                    result.relevance,
                    result.urgency,
                    result.suggested_action,
                    result.candidate_strategy.value,
                    result.direction.value,
                    result.event_type,
                    result.price_impact_magnitude,
                    result.reasoning,
                    result.confidence,
                    Json(_article_snapshot(article)),
                    market_status,
                    market_as_of,
                    Json(market_context_snapshot) if market_context_snapshot is not None else None,
                    result.is_market_moving,
                    result.content_type.value,
                    validation_status.value,
                    Json(validation_errors),
                    rejection_reason_code,
                    result.llm_model,
                    result.estimated_tokens,
                    datetime.now(timezone.utc),
                ),
            )
            return int(cur.fetchone()[0])

    def _update_window_and_maybe_create_card(
        self,
        *,
        conn: psycopg.Connection,
        analysis_id: int,
        article: NewsArticle,
        result: LlmAnalysisResult,
        market_context_snapshot: dict[str, Any] | None,
        required_evidence_count: int,
        risk_max_loss_usd: float,
        default_time_horizon: str,
        evidence_collection_max_minutes: int,
        max_evidence_age_minutes: int,
        clock: Callable[[], datetime] | None = None,
        reprocess_run_id: str | None = None,
    ) -> ThesisCardSignal | None:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        real_now = datetime.now(timezone.utc)
        window = self._load_or_create_window(conn=conn, result=result, article=article, analysis_id=analysis_id, now=now, required_evidence_count=required_evidence_count, reprocess_run_id=reprocess_run_id)
        article_ids = list(dict.fromkeys([*window["article_ids"], article.id]))
        analysis_ids = list(dict.fromkeys([*window["analysis_ids"], analysis_id]))
        status = "collecting"
        status_reason = None
        if now - window["window_started_at"] > timedelta(minutes=evidence_collection_max_minutes):
            status = "expired"
            status_reason = "evidence_window_expired"
        self._update_window(
            conn=conn,
            window_id=int(window["id"]),
            article_ids=article_ids,
            analysis_ids=analysis_ids,
            last_evidence_at=max(_to_utc(article.published_at), window["last_evidence_at"]),
            status=status,
            status_reason=status_reason,
        )
        if status != "collecting" or len(set(article_ids)) < required_evidence_count:
            return None

        analyses = self._load_valid_analyses(conn=conn, analysis_ids=analysis_ids)
        unique_article_ids = list(dict.fromkeys(analysis.article_id for analysis in analyses))
        if len(unique_article_ids) < required_evidence_count or len(unique_article_ids) < 2:
            return None
        selected = analyses[:required_evidence_count]
        selected_article_ids = [analysis.article_id for analysis in selected]
        evidence = _evidence(selected=selected)
        max_age_seconds = max((now - _to_utc(item.article.published_at)).total_seconds() for item in selected)
        allowed_age_seconds = max_evidence_age_minutes * 60
        stale_seconds = max(0.0, max_age_seconds - allowed_age_seconds)
        validation_status = ValidationStatus.REJECTED if stale_seconds > 0 else ValidationStatus.VALID
        rejection_reason = "stale_evidence" if stale_seconds > 0 else None
        confidence = sum(item.confidence for item in selected) / len(selected)
        idempotency_key = _card_idempotency_key(
            ticker=result.ticker,
            exchange_code=result.exchange_code,
            strategy=result.candidate_strategy.value,
            direction=result.direction.value,
            article_ids=selected_article_ids,
            reprocess_run_id=reprocess_run_id,
        )
        card_id = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
        created_at = now
        expires_at = real_now + timedelta(hours=6)
        risk_box = {
            "max_loss_usd": risk_max_loss_usd,
            "stop_condition": _stop_condition(result),
            "invalidation_condition": _invalidation_condition(result),
        }
        inserted = self._insert_card(
            conn=conn,
            card_id=card_id,
            idempotency_key=idempotency_key,
            result=result,
            evidence=evidence,
            source_analysis_ids=[item.id for item in selected],
            confidence=confidence,
            risk_box=risk_box,
            market_context_snapshot=market_context_snapshot,
            validation_status=validation_status,
            rejection_reason_code=rejection_reason,
            max_evidence_age_seconds=max_age_seconds,
            allowed_max_evidence_age_seconds=allowed_age_seconds,
            evidence_age_exceeded_seconds=stale_seconds,
            expires_at=expires_at,
            created_at=created_at,
            default_time_horizon=default_time_horizon,
        )
        self._update_window(conn=conn, window_id=int(window["id"]), article_ids=article_ids, analysis_ids=analysis_ids, last_evidence_at=now, status="satisfied", status_reason="thesis_card_created")
        if validation_status is ValidationStatus.REJECTED:
            return None
        signal = self._load_unpublished_signal(conn=conn, card_id=card_id)
        if not inserted:
            return None
        return signal

    def _load_or_create_window(
        self,
        *,
        conn: psycopg.Connection,
        result: LlmAnalysisResult,
        article: NewsArticle,
        analysis_id: int,
        now: datetime,
        required_evidence_count: int,
        reprocess_run_id: str | None = None,
    ) -> dict[str, Any]:
        select_sql = (
            f"SELECT id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count "
            f"FROM {self._thesis_schema}.t_evidence_windows "
            f"WHERE ticker = %s AND exchange_code = %s AND strategy = %s "
            f"AND COALESCE(direction, '') = COALESCE(%s, '') AND status = 'collecting' "
            f"AND COALESCE(reprocess_run_id, '') = COALESCE(%s, '') "
            f"LIMIT 1"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                select_sql,
                (result.ticker, result.exchange_code, result.candidate_strategy.value, result.direction.value, reprocess_run_id),
            )
            row = cur.fetchone()
            if row is not None:
                return _window(row)
            insert_sql = (
                f"INSERT INTO {self._thesis_schema}.t_evidence_windows "
                f"(ticker, exchange_code, strategy, direction, article_ids, analysis_ids, window_started_at, last_evidence_at, status, reprocess_run_id, required_evidence_count) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'collecting', %s, %s) RETURNING id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count"
            )
            cur.execute(
                insert_sql,
                (
                    result.ticker,
                    result.exchange_code,
                    result.candidate_strategy.value,
                    result.direction.value,
                    Json([article.id]),
                    Json([analysis_id]),
                    _to_utc(article.published_at),
                    _to_utc(article.published_at),
                    reprocess_run_id,
                    required_evidence_count,
                ),
            )
            return _window(cur.fetchone())

    def _update_window(
        self,
        *,
        conn: psycopg.Connection,
        window_id: int,
        article_ids: list[str],
        analysis_ids: list[int],
        last_evidence_at: datetime,
        status: str,
        status_reason: str | None,
    ) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_evidence_windows "
            f"SET article_ids = %s, analysis_ids = %s, last_evidence_at = %s, status = %s, status_reason = %s, updated_at = NOW() "
            f"WHERE id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (Json(article_ids), Json(analysis_ids), _to_utc(last_evidence_at), status, status_reason, window_id))

    def _load_valid_analyses(self, *, conn: psycopg.Connection, analysis_ids: list[int]) -> list[PersistedAnalysis]:
        sql = (
            f"SELECT id, article_id, article_snapshot, ticker, exchange_code, strategy, direction, confidence, reasoning, "
            f"validation_status, rejection_reason_code, analyzed_at "
            f"FROM {self._thesis_schema}.t_news_analyses "
            f"WHERE id = ANY(%s) AND validation_status = 'valid' "
            f"ORDER BY analyzed_at, id"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (analysis_ids,))
            rows = cur.fetchall()
        return [_analysis(row) for row in rows]

    def _insert_card(self, *, conn: psycopg.Connection, card_id: str, idempotency_key: str, result: LlmAnalysisResult, evidence: list[dict[str, Any]], source_analysis_ids: list[int], confidence: float, risk_box: dict[str, Any], market_context_snapshot: dict[str, Any] | None, validation_status: ValidationStatus, rejection_reason_code: str | None, max_evidence_age_seconds: float, allowed_max_evidence_age_seconds: float, evidence_age_exceeded_seconds: float, expires_at: datetime, created_at: datetime, default_time_horizon: str) -> bool:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_thesis_cards "
            f"(id, idempotency_key, ticker, exchange_code, direction, time_horizon, strategy, evidence, source_analysis_ids, confidence, "
            f"risk_max_loss_usd, risk_stop_condition, risk_invalidation_condition, market_context_status, market_context_as_of, market_context_snapshot, "
            f"validation_status, validation_errors, rejection_reason_code, max_evidence_age_seconds, allowed_max_evidence_age_seconds, evidence_age_exceeded_seconds, expires_at, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (idempotency_key) DO NOTHING"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    card_id,
                    idempotency_key,
                    result.ticker,
                    result.exchange_code,
                    result.direction.value,
                    default_time_horizon,
                    result.candidate_strategy.value,
                    Json(evidence),
                    Json(source_analysis_ids),
                    confidence,
                    risk_box["max_loss_usd"],
                    risk_box["stop_condition"],
                    risk_box["invalidation_condition"],
                    _market_context_status(market_context_snapshot),
                    _market_context_as_of(market_context_snapshot),
                    Json(market_context_snapshot) if market_context_snapshot is not None else None,
                    validation_status.value,
                    Json([rejection_reason_code] if rejection_reason_code else []),
                    rejection_reason_code,
                    max_evidence_age_seconds,
                    allowed_max_evidence_age_seconds,
                    evidence_age_exceeded_seconds,
                    _to_utc(expires_at),
                    _to_utc(created_at),
                ),
            )
            return cur.rowcount == 1

    def _load_unpublished_signal(self, *, conn: psycopg.Connection, card_id: str) -> ThesisCardSignal | None:
        sql = (
            f"SELECT id, ticker, exchange_code, direction, time_horizon, strategy, confidence, "
            f"risk_max_loss_usd, risk_stop_condition, risk_invalidation_condition, source_analysis_ids, created_at, expires_at "
            f"FROM {self._thesis_schema}.t_thesis_cards "
            f"WHERE id = %s AND validation_status = 'valid' AND direction <> 'hold' "
            f"AND signal_published_at IS NULL AND expires_at > NOW()"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (card_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return ThesisCardSignal(
            thesis_card_id=str(row["id"]),
            ticker=str(row["ticker"]),
            exchange_code=str(row["exchange_code"]),
            direction=str(row["direction"]),
            time_horizon=str(row["time_horizon"]),
            strategy=str(row["strategy"]),
            confidence=float(row["confidence"]),
            risk_box={
                "max_loss_usd": float(row["risk_max_loss_usd"]),
                "stop_condition": str(row["risk_stop_condition"]),
                "invalidation_condition": str(row["risk_invalidation_condition"]),
            },
            source_analysis_ids=[int(item) for item in row["source_analysis_ids"]],
            created_at=_to_utc(row["created_at"]),
            expires_at=_to_utc(row["expires_at"]),
        )

    def insert_reprocess_run(self, *, run_id: str, days_back: int) -> None:
        """Insert a reprocess run in the 'accepted' state.

        Raises ReprocessRunAlreadyActive if another accepted/running run exists
        (enforced by the uq_reprocess_runs_active partial unique index).
        """
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_reprocess_runs "
            f"(run_id, days_back, status, requested_at) "
            f"VALUES (%s, %s, 'accepted', NOW())"
        )
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, (run_id, days_back))
                conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                if "uq_reprocess_runs_active" in str(exc):
                    raise ReprocessRunAlreadyActive() from exc
                raise

    def mark_reprocess_running(self, *, run_id: str, max_articles: int) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_reprocess_runs "
            f"SET status = 'running', max_articles = %s, started_at = NOW() "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (max_articles, run_id))
            conn.commit()

    def mark_reprocess_completed(
        self,
        *,
        run_id: str,
        articles_found: int,
        analyses_created: int,
        cards_created: int,
    ) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_reprocess_runs "
            f"SET status = 'completed', articles_found = %s, analyses_created = %s, "
            f"cards_created = %s, finished_at = NOW() "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (articles_found, analyses_created, cards_created, run_id))
            conn.commit()

    def mark_reprocess_failed(self, *, run_id: str, error_code: str) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_reprocess_runs "
            f"SET status = 'failed', error_code = %s, finished_at = NOW() "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (error_code[:200], run_id))
            conn.commit()

    def get_reprocess_run(self, *, run_id: str) -> ReprocessRunRecord | None:
        sql = (
            f"SELECT run_id, days_back, max_articles, status, articles_found, analyses_created, "
            f"cards_created, error_code, requested_at, started_at, finished_at "
            f"FROM {self._thesis_schema}.t_reprocess_runs "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (run_id,))
                row = cur.fetchone()
        return _reprocess_run(row) if row else None

    def get_active_reprocess_run(self) -> ReprocessRunRecord | None:
        sql = (
            f"SELECT run_id, days_back, max_articles, status, articles_found, analyses_created, "
            f"cards_created, error_code, requested_at, started_at, finished_at "
            f"FROM {self._thesis_schema}.t_reprocess_runs "
            f"WHERE status IN ('accepted', 'running') "
            f"ORDER BY requested_at DESC LIMIT 1"
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                row = cur.fetchone()
        return _reprocess_run(row) if row else None

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def _reprocess_run(row: dict[str, Any]) -> ReprocessRunRecord:
    return ReprocessRunRecord(
        run_id=str(row["run_id"]),
        days_back=int(row["days_back"]),
        max_articles=int(row["max_articles"]) if row["max_articles"] is not None else None,
        status=str(row["status"]),
        articles_found=int(row["articles_found"]) if row["articles_found"] is not None else None,
        analyses_created=int(row["analyses_created"]) if row["analyses_created"] is not None else None,
        cards_created=int(row["cards_created"]) if row["cards_created"] is not None else None,
        error_code=row["error_code"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _analysis_rejection(
    result: LlmAnalysisResult, *, min_confidence: float, min_relevance: float = 0.0
) -> str | None:
    # Discard articles that are not genuinely about this instrument first, so incidental
    # name-drops (e.g. a comparison mention in another company's story) are dropped as noise
    # rather than retained for the analyst.
    if not result.instrument_is_subject:
        return "instrument_not_subject"
    # Only news-catalyst articles may drive a thesis. Opinion articles that ARE about this
    # instrument never enter the evidence window; they are retained (validation_status=
    # 'rejected', but queryable via content_type) for a future stock-analyst component, which
    # will do the finer-grained grounded/ungrounded classification using the full article text.
    if result.content_type is ContentType.OPINION:
        return "routed_to_analyst"
    if result.candidate_strategy not in _SUPPORTED_EXECUTABLE_STRATEGIES:
        return f"unsupported_strategy_{result.candidate_strategy.value}"
    if result.relevance < min_relevance:
        return "below_min_relevance"
    if result.direction is TradeDirection.HOLD:
        return "hold_not_executable"
    if not result.is_market_moving:
        return "not_market_moving"
    if result.confidence < min_confidence:
        return "below_min_confidence"
    return None


def _evidence(*, selected: list[PersistedAnalysis]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for analysis in selected:
        article = analysis.article
        bullet = (analysis.reasoning or article.summary or article.headline).strip()
        evidence.append(
            {
                "bullet": bullet[:500],
                "article_id": article.id,
                "source": article.source,
                "published_at": _to_utc(article.published_at).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return evidence


def _card_idempotency_key(*, ticker: str, exchange_code: str, strategy: str, direction: str, article_ids: list[str], reprocess_run_id: str | None = None) -> str:
    parts = [ticker, exchange_code, strategy, direction, *sorted(article_ids)]
    if reprocess_run_id is not None:
        parts.append(reprocess_run_id)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _stop_condition(result: LlmAnalysisResult) -> str:
    if result.direction is TradeDirection.BUY:
        return "negative_followup_news_or_close_below_recent_support"
    if result.direction is TradeDirection.SELL:
        return "positive_followup_news_or_close_above_recent_resistance"
    return "no_trade_hold"


def _invalidation_condition(result: LlmAnalysisResult) -> str:
    if result.candidate_strategy is ThesisStrategy.EVENT_DRIVEN:
        return "event_thesis_reversed_or_materially_contradicted"
    return "sentiment_momentum_reversed_or_materially_contradicted"


def _market_context_status(snapshot: dict[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    value = snapshot.get("source_status")
    return str(value.value if hasattr(value, "value") else value) if value is not None else None


def _market_context_as_of(snapshot: dict[str, Any] | None) -> datetime | None:
    if snapshot is None or snapshot.get("as_of") is None:
        return None
    value = snapshot["as_of"]
    return _to_utc(value) if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _window(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "article_ids": list(row["article_ids"] or []),
        "analysis_ids": [int(item) for item in row["analysis_ids"] or []],
        "window_started_at": _to_utc(row["window_started_at"]),
        "last_evidence_at": _to_utc(row["last_evidence_at"]),
        "required_evidence_count": int(row["required_evidence_count"] or 3),
    }


def _article(row: dict[str, Any]) -> NewsArticle:
    published_at = row["published_at"]
    fetched_at = row["fetched_at"]
    return NewsArticle(
        id=str(row["id"]),
        source=str(row["source"]),
        headline=str(row["headline"]),
        summary=row["summary"],
        url=str(row["url"]),
        tickers=[str(item).strip().upper() for item in (row["tickers"] or []) if str(item).strip()],
        published_at=_to_utc(published_at) if isinstance(published_at, datetime) else datetime.fromisoformat(str(published_at).replace("Z", "+00:00")),
        fetched_at=_to_utc(fetched_at) if isinstance(fetched_at, datetime) else datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00")),
        sentiment_source=row["sentiment_source"],
    )


def _analysis(row: dict[str, Any]) -> PersistedAnalysis:
    article_snapshot = dict(row["article_snapshot"] or {})
    return PersistedAnalysis(
        id=int(row["id"]),
        article_id=str(row["article_id"]),
        article=_article(article_snapshot),
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        strategy=ThesisStrategy(str(row["strategy"])) if row["strategy"] else None,
        direction=TradeDirection(str(row["direction"])) if row["direction"] else None,
        confidence=float(row["confidence"]),
        reasoning=row["reasoning"],
        validation_status=ValidationStatus(str(row["validation_status"])),
        rejection_reason_code=row["rejection_reason_code"],
        analyzed_at=_to_utc(row["analyzed_at"]),
    )


def _article_snapshot(article: NewsArticle) -> dict[str, Any]:
    return {
        "id": article.id,
        "source": article.source,
        "headline": article.headline,
        "summary": article.summary,
        "url": article.url,
        "tickers": list(article.tickers),
        "published_at": _to_utc(article.published_at).isoformat(),
        "fetched_at": _to_utc(article.fetched_at).isoformat(),
        "sentiment_source": article.sentiment_source,
    }


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
