from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.product_components.shared.text_match import contains_term

from .models import (
    ContentType,
    InstrumentIdentity,
    LlmAnalysisResult,
    LlmSynthesisResult,
    LlmTriageResult,
    NewsArticle,
    PersistedAnalysis,
    StoryAssignmentCandidate,
    SubjectRelation,
    ThesisCardSignal,
    ThesisStrategy,
    TradeDirection,
    ValidationStatus,
)
from .event_identity import compare_event_identity, taxonomy_gap_values
from .taxonomy_decisions import TaxonomyDecisionRequest, TaxonomyDecisionValidationError
from .taxonomy_gateway import TaxonomyBackfillStatus, TaxonomyCommand
from .taxonomy_runtime import TaxonomyValue, family_rules_scope
from .taxonomy_worker import (
    TaxonomyBackfillAnalysis,
    TaxonomyBackfillJob,
    TaxonomyCommandRetryableError,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_EXECUTABLE_STRATEGIES = {
    ThesisStrategy.EVENT_DRIVEN,
    ThesisStrategy.SENTIMENT_MOMENTUM,
}
_DIRECT_TEXT_DOWNGRADE_AUDIT = "direct_subject_text_mismatch_downgraded"
_STORY_GENERIC_TOKENS = {
    "about",
    "after",
    "against",
    "announces",
    "announcement",
    "business",
    "capacity",
    "company",
    "deal",
    "demand",
    "event",
    "expansion",
    "guidance",
    "major",
    "market",
    "markets",
    "news",
    "partnership",
    "platform",
    "profit",
    "revenue",
    "sales",
    "sector",
    "shares",
    "stock",
    "story",
    "supplier",
    "supports",
    "technology",
    "today",
    "unveils",
    # grammar / filler words (>=4 chars) that create spurious overlap
    "that",
    "this",
    "these",
    "those",
    "with",
    "will",
    "from",
    "into",
    "over",
    "than",
    "then",
    "amid",
    "also",
    "more",
    "most",
    "such",
    "been",
    "have",
    "they",
    "their",
    "were",
    "does",
    "could",
    "would",
    "should",
    "here",
    "what",
    "when",
    "which",
    "while",
    "according",
    # generic finance / market vocabulary (not story-identifying)
    "price",
    "prices",
    "billion",
    "million",
    "trillion",
    "record",
    "launch",
    "launches",
    "report",
    "reports",
    "reported",
    "plans",
    "year",
    "week",
    "quarter",
    "results",
    "result",
    "analyst",
    "analysts",
    "upside",
    "target",
    "wall",
    "street",
    "gains",
    "growth",
    "investment",
    "investors",
    "strong",
    "expected",
    "significant",
    "buy",
    "next",
    "deploy",
    "deployment",
    "funding",
    "raise",
    "offering",
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


@dataclass(frozen=True)
class TaxonomyDecisionRecord:
    command_id: str
    gap_id: int
    action: str
    status: str
    taxonomy_revision: int | None
    error_code: str | None = None


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
        card_synthesizer: Any | None = None,
        story_assigner: Any | None = None,
    ) -> None:
        self._dsn = dsn
        self._thesis_schema = _safe_identifier(thesis_schema)
        self._card_synthesizer = card_synthesizer
        self._story_assigner = story_assigner

    def persist_rejected_analysis(
        self,
        *,
        article: NewsArticle,
        instrument: InstrumentIdentity,
        rejection_reason_code: str,
        llm_model: str,
        validation_errors: list[Any],
        triage_result: LlmTriageResult | None = None,
    ) -> int:
        reasoning = rejection_reason_code
        estimated_tokens = 0
        content_type = ContentType.OPINION
        instrument_is_subject = False
        if triage_result is not None:
            reasoning = triage_result.reasoning or rejection_reason_code
            estimated_tokens = triage_result.estimated_tokens
            content_type = triage_result.content_type
            instrument_is_subject = triage_result.instrument_is_subject
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
            reasoning=reasoning,
            is_market_moving=False,
            instrument_is_subject=instrument_is_subject,
            content_type=content_type,
            subject_relation=SubjectRelation.DIRECT if instrument_is_subject else SubjectRelation.NONE,
            llm_model=llm_model,
            estimated_tokens=estimated_tokens,
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
        fundamentals_snapshot: dict[str, Any] | None = None,
        instrument_display_name: str | None = None,
        instrument_aliases: tuple[str, ...] = (),
        required_evidence_count: int,
        min_confidence: float,
        min_relevance: float = 0.0,
        risk_max_loss_usd: float,
        tradeability_max_entry_price: float = 1000.0,
        tradeability_atr_stop_mult: float = 1.5,
        default_time_horizon: str,
        evidence_collection_max_minutes: int,
        max_evidence_age_minutes: int,
        already_priced_event_driven_atr_multiple: float,
        already_priced_event_driven_return_threshold: float,
        already_priced_sentiment_momentum_atr_multiple: float,
        already_priced_sentiment_momentum_return_threshold: float,
        synthesis_enabled: bool = False,
        synthesis_model: str | None = None,
        synthesis_max_output_tokens: int | None = None,
        synthesis_fallback_to_mechanical: bool = False,
        story_scoping_enabled: bool = False,
        story_assignment_model: str | None = None,
        story_assignment_max_output_tokens: int | None = None,
        clock: Callable[[], datetime] | None = None,
        reprocess_run_id: str | None = None,
    ) -> AnalysisPersistenceResult:
        original_result = result
        result = _normalize_analysis_result(
            result,
            article=article,
            instrument_display_name=instrument_display_name,
            instrument_aliases=instrument_aliases,
        )
        normalization_errors = _normalization_validation_errors(original_result, result)
        with self._connect() as conn:
            now = clock() if clock is not None else datetime.now(timezone.utc)
            has_anchor_evidence = True if story_scoping_enabled else _has_anchor_evidence(
                conn=conn,
                schema=self._thesis_schema,
                result=result,
                reprocess_run_id=reprocess_run_id,
                now=now,
            )
            rejection = _analysis_rejection(
                result,
                min_confidence=min_confidence,
                min_relevance=min_relevance,
                has_anchor_evidence=has_anchor_evidence,
            )
            status = ValidationStatus.REJECTED if rejection else ValidationStatus.VALID
            analysis_id = self._insert_analysis(
                conn=conn,
                article=article,
                result=result,
                validation_status=status,
                rejection_reason_code=rejection,
                validation_errors=[
                    *normalization_errors,
                    *([rejection] if rejection else []),
                ],
                market_context_snapshot=market_context_snapshot,
                fundamentals_snapshot=fundamentals_snapshot,
            )
            signal = None
            if status is ValidationStatus.VALID:
                signal = self._update_window_and_maybe_create_card(
                    conn=conn,
                    analysis_id=analysis_id,
                    article=article,
                    result=result,
                    market_context_snapshot=market_context_snapshot,
                    instrument_display_name=instrument_display_name,
                    instrument_aliases=instrument_aliases,
                    required_evidence_count=required_evidence_count,
                    risk_max_loss_usd=risk_max_loss_usd,
                    tradeability_max_entry_price=tradeability_max_entry_price,
                    tradeability_atr_stop_mult=tradeability_atr_stop_mult,
                    default_time_horizon=default_time_horizon,
                    evidence_collection_max_minutes=evidence_collection_max_minutes,
                    max_evidence_age_minutes=max_evidence_age_minutes,
                    already_priced_event_driven_atr_multiple=already_priced_event_driven_atr_multiple,
                    already_priced_event_driven_return_threshold=already_priced_event_driven_return_threshold,
                    already_priced_sentiment_momentum_atr_multiple=already_priced_sentiment_momentum_atr_multiple,
                    already_priced_sentiment_momentum_return_threshold=already_priced_sentiment_momentum_return_threshold,
                    synthesis_enabled=synthesis_enabled,
                    synthesis_model=synthesis_model,
                    synthesis_max_output_tokens=synthesis_max_output_tokens,
                    synthesis_fallback_to_mechanical=synthesis_fallback_to_mechanical,
                    story_scoping_enabled=story_scoping_enabled,
                    story_assignment_model=story_assignment_model,
                    story_assignment_max_output_tokens=story_assignment_max_output_tokens,
                    clock=lambda: now,
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
        fundamentals_snapshot: dict[str, Any] | None = None,
    ) -> int:
        market_status = _market_context_status(market_context_snapshot)
        market_as_of = _market_context_as_of(market_context_snapshot)
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_news_analyses "
            f"(article_id, ticker, exchange_code, sentiment, relevance, urgency, suggested_action, "
            f"strategy, direction, event_type, event_identity_json, taxonomy_revision, subject_relation, event_occurred_at, price_impact_magnitude, impact_horizon, reasoning, confidence, article_snapshot, "
            f"market_context_status, market_context_as_of, market_context_snapshot, fundamentals_snapshot, is_market_moving, "
            f"content_type, validation_status, validation_errors, rejection_reason_code, llm_model, tokens_used, analyzed_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
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
                    Json(result.event_identity),
                    int(result.event_identity.get("taxonomy_revision") or 1),
                    result.subject_relation.value,
                    _to_utc(result.event_occurred_at) if result.event_occurred_at else None,
                    result.price_impact_magnitude,
                    result.impact_horizon,
                    result.reasoning,
                    result.confidence,
                    Json(_article_snapshot(article)),
                    market_status,
                    market_as_of,
                    Json(market_context_snapshot) if market_context_snapshot is not None else None,
                    Json(fundamentals_snapshot) if fundamentals_snapshot is not None else None,
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
            analysis_id = int(cur.fetchone()[0])
        self._upsert_taxonomy_gaps(conn=conn, analysis_id=analysis_id, article=article, identity=result.event_identity)
        return analysis_id

    def _upsert_taxonomy_gaps(self, *, conn: psycopg.Connection, analysis_id: int, article: NewsArticle, identity: dict[str, Any]) -> None:
        for dimension, proposal in taxonomy_gap_values(identity):
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_gaps (dimension, raw_value, normalized_proposal, occurrence_count, first_seen_at, last_seen_at, representative_analysis_ids, representative_headlines) "
                    f"VALUES (%s, %s, %s, 1, NOW(), NOW(), %s, %s) "
                    f"ON CONFLICT (dimension, normalized_proposal) DO UPDATE SET occurrence_count = {self._thesis_schema}.t_event_taxonomy_gaps.occurrence_count + 1, last_seen_at = NOW(), representative_analysis_ids = CASE WHEN jsonb_array_length({self._thesis_schema}.t_event_taxonomy_gaps.representative_analysis_ids) < 5 THEN {self._thesis_schema}.t_event_taxonomy_gaps.representative_analysis_ids || EXCLUDED.representative_analysis_ids ELSE {self._thesis_schema}.t_event_taxonomy_gaps.representative_analysis_ids END, representative_headlines = CASE WHEN jsonb_array_length({self._thesis_schema}.t_event_taxonomy_gaps.representative_headlines) < 5 THEN {self._thesis_schema}.t_event_taxonomy_gaps.representative_headlines || EXCLUDED.representative_headlines ELSE {self._thesis_schema}.t_event_taxonomy_gaps.representative_headlines END",
                    (dimension, proposal[:120], proposal[:120], Json([analysis_id]), Json([article.headline[:240]])),
                )

    def _update_window_and_maybe_create_card(
        self,
        *,
        conn: psycopg.Connection,
        analysis_id: int,
        article: NewsArticle,
        result: LlmAnalysisResult,
        market_context_snapshot: dict[str, Any] | None,
        instrument_display_name: str | None = None,
        instrument_aliases: tuple[str, ...] = (),
        required_evidence_count: int,
        risk_max_loss_usd: float,
        tradeability_max_entry_price: float,
        tradeability_atr_stop_mult: float,
        default_time_horizon: str,
        evidence_collection_max_minutes: int,
        max_evidence_age_minutes: int,
        already_priced_event_driven_atr_multiple: float,
        already_priced_event_driven_return_threshold: float,
        already_priced_sentiment_momentum_atr_multiple: float,
        already_priced_sentiment_momentum_return_threshold: float,
        synthesis_enabled: bool = False,
        synthesis_model: str | None = None,
        synthesis_max_output_tokens: int | None = None,
        synthesis_fallback_to_mechanical: bool = False,
        story_scoping_enabled: bool = False,
        story_assignment_model: str | None = None,
        story_assignment_max_output_tokens: int | None = None,
        clock: Callable[[], datetime] | None = None,
        reprocess_run_id: str | None = None,
    ) -> ThesisCardSignal | None:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        real_now = datetime.now(timezone.utc)
        if story_scoping_enabled:
            target = self._resolve_story_target(
                conn=conn,
                article=article,
                result=result,
                analysis_id=analysis_id,
                now=now,
                required_evidence_count=required_evidence_count,
                story_assignment_model=story_assignment_model,
                story_assignment_max_output_tokens=story_assignment_max_output_tokens,
                reprocess_run_id=reprocess_run_id,
                instrument_display_name=instrument_display_name,
                instrument_aliases=instrument_aliases,
            )
            if target["target_type"] == "card":
                self._insert_card_corroboration(
                    conn=conn,
                    card_id=str(target["target_id"]),
                    article_id=article.id,
                    analysis_id=analysis_id,
                    matched_at=now,
                )
                return None
            if not _target_has_anchor_evidence(target=target, result=result):
                self._reject_analysis(
                    conn=conn,
                    analysis_id=analysis_id,
                    rejection_reason_code="indirect_no_anchor_evidence",
                )
                return None
            if target["target_type"] == "new_story":
                window = self._create_story_window(
                    conn=conn,
                    result=result,
                    article=article,
                    analysis_id=analysis_id,
                    required_evidence_count=required_evidence_count,
                    reprocess_run_id=reprocess_run_id,
                )
            else:
                window = target["window"]
        else:
            window = self._load_or_create_window(conn=conn, result=result, article=article, analysis_id=analysis_id, now=now, required_evidence_count=required_evidence_count, reprocess_run_id=reprocess_run_id)
        candidate_analysis_ids = list(dict.fromkeys([*window["analysis_ids"], analysis_id]))
        analyses = self._load_valid_analyses(conn=conn, analysis_ids=candidate_analysis_ids)
        # Rolling window: evidence published more than evidence_collection_max_minutes
        # before `now` ages out individually; the window itself never expires, so a new
        # arrival always lands in live collecting state instead of being swallowed by a
        # window that expired underneath it. The arrival itself is always retained —
        # card-level freshness is still enforced by max_evidence_age_minutes below.
        cutoff = now - timedelta(minutes=evidence_collection_max_minutes)
        retained = [
            item
            for item in analyses
            if item.id == analysis_id or _effective_evidence_at(item) >= cutoff
        ]
        seen_article_ids: set[str] = set()
        unique_by_article: list[PersistedAnalysis] = []
        for item in retained:
            if item.article_id in seen_article_ids:
                continue
            seen_article_ids.add(item.article_id)
            unique_by_article.append(item)
        article_ids = [item.article_id for item in unique_by_article]
        analysis_ids = [item.id for item in retained]
        evidence_ats = [_effective_evidence_at(item) for item in retained]
        window_started_at = min(evidence_ats)
        self._update_window(
            conn=conn,
            window_id=int(window["id"]),
            article_ids=article_ids,
            analysis_ids=analysis_ids,
            window_started_at=window_started_at,
            last_evidence_at=max(evidence_ats),
            status="collecting",
            status_reason=None,
        )
        if len(article_ids) < required_evidence_count or len(article_ids) < 2:
            return None

        selected = unique_by_article[:required_evidence_count]
        if not any(_is_direct_relation(analysis.subject_relation) for analysis in selected):
            return None
        selected_article_ids = [analysis.article_id for analysis in selected]
        evidence = _evidence(selected=selected)
        max_age_seconds = max((now - _effective_evidence_at(item)).total_seconds() for item in selected)
        published_max_age_seconds = max(
            (now - _to_utc(item.article.published_at)).total_seconds() for item in selected
        )
        allowed_age_seconds = max_evidence_age_minutes * 60
        stale_seconds = max(0.0, max_age_seconds - allowed_age_seconds)
        validation_status = ValidationStatus.REJECTED if stale_seconds > 0 else ValidationStatus.VALID
        rejection_reason = None
        if stale_seconds > 0:
            published_stale_seconds = max(0.0, published_max_age_seconds - allowed_age_seconds)
            rejection_reason = "stale_event" if published_stale_seconds == 0 else "stale_evidence"
        already_priced_rejection = _already_priced_rejection(
            result=result,
            market_context_snapshot=market_context_snapshot,
            event_driven_atr_multiple=already_priced_event_driven_atr_multiple,
            event_driven_return_threshold=already_priced_event_driven_return_threshold,
            sentiment_momentum_atr_multiple=already_priced_sentiment_momentum_atr_multiple,
            sentiment_momentum_return_threshold=already_priced_sentiment_momentum_return_threshold,
        )
        if validation_status is ValidationStatus.VALID and already_priced_rejection is not None:
            validation_status = ValidationStatus.REJECTED
            rejection_reason = already_priced_rejection
        tradeability_rejection = _tradeability_rejection(
            market_context_snapshot=market_context_snapshot,
            tradeability_max_entry_price=tradeability_max_entry_price,
            risk_max_loss_usd=risk_max_loss_usd,
            atr_stop_mult=tradeability_atr_stop_mult,
        )
        if validation_status is ValidationStatus.VALID and tradeability_rejection is not None:
            validation_status = ValidationStatus.REJECTED
            rejection_reason = tradeability_rejection
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
        synthesis_result: LlmSynthesisResult | None = None
        if validation_status is ValidationStatus.VALID and synthesis_enabled:
            try:
                if self._card_synthesizer is None:
                    raise RuntimeError("synthesis_unavailable")
                synthesis_result = self._card_synthesizer.synthesize(
                    dossier=_synthesis_dossier(
                        result=result,
                        selected=selected,
                        evidence=evidence,
                        market_context_snapshot=market_context_snapshot,
                        risk_box=risk_box,
                        default_time_horizon=default_time_horizon,
                    )
                )
            except ValueError as exc:
                if not synthesis_fallback_to_mechanical:
                    self._insert_synthesis_verdict(
                        conn=conn,
                        evidence_window_id=int(window["id"]),
                        card_id=None,
                        result=result,
                        verdict="invalid",
                        reason_code=str(exc) or "synthesis_invalid",
                        confidence=None,
                        llm_model=synthesis_model,
                        max_output_tokens=synthesis_max_output_tokens,
                        response_json={},
                    )
                    self._update_window(conn=conn, window_id=int(window["id"]), article_ids=article_ids, analysis_ids=analysis_ids, window_started_at=window_started_at, last_evidence_at=now, status="rejected", status_reason="synthesis_invalid")
                    return None
            except Exception as exc:
                if not synthesis_fallback_to_mechanical:
                    self._insert_synthesis_verdict(
                        conn=conn,
                        evidence_window_id=int(window["id"]),
                        card_id=None,
                        result=result,
                        verdict="unavailable",
                        reason_code=str(exc) or "synthesis_unavailable",
                        confidence=None,
                        llm_model=synthesis_model,
                        max_output_tokens=synthesis_max_output_tokens,
                        response_json={},
                    )
                    self._update_window(conn=conn, window_id=int(window["id"]), article_ids=article_ids, analysis_ids=analysis_ids, window_started_at=window_started_at, last_evidence_at=now, status="rejected", status_reason="synthesis_unavailable")
                    return None
            if synthesis_result is not None:
                if synthesis_result.verdict == "reject":
                    self._insert_synthesis_verdict(
                        conn=conn,
                        evidence_window_id=int(window["id"]),
                        card_id=None,
                        result=result,
                        verdict="reject",
                        reason_code=synthesis_result.reason_code or "synthesis_rejected",
                        confidence=synthesis_result.confidence,
                        llm_model=synthesis_result.llm_model or synthesis_model,
                        max_output_tokens=synthesis_max_output_tokens,
                        response_json=synthesis_result.raw_response,
                    )
                    self._update_window(conn=conn, window_id=int(window["id"]), article_ids=article_ids, analysis_ids=analysis_ids, window_started_at=window_started_at, last_evidence_at=now, status="rejected", status_reason="synthesis_rejected")
                    return None
                confidence = synthesis_result.confidence
                evidence = _synthesized_evidence(synthesis_result)
                risk_box = {
                    "max_loss_usd": risk_max_loss_usd,
                    "stop_condition": synthesis_result.risk_stop_condition,
                    "invalidation_condition": synthesis_result.risk_invalidation_condition,
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
            story_narrative=window.get("story_narrative"),
        )
        if synthesis_result is not None:
            self._insert_synthesis_verdict(
                conn=conn,
                evidence_window_id=int(window["id"]),
                card_id=card_id,
                result=result,
                verdict="approve",
                reason_code=synthesis_result.reason_code,
                confidence=synthesis_result.confidence,
                llm_model=synthesis_result.llm_model or synthesis_model,
                max_output_tokens=synthesis_max_output_tokens,
                response_json=synthesis_result.raw_response,
            )
        self._update_window(conn=conn, window_id=int(window["id"]), article_ids=article_ids, analysis_ids=analysis_ids, window_started_at=window_started_at, last_evidence_at=now, status="satisfied", status_reason="thesis_card_created")
        if validation_status is ValidationStatus.REJECTED:
            return None
        signal = self._load_unpublished_signal(conn=conn, card_id=card_id)
        if not inserted:
            return None
        return signal

    def _resolve_story_target(
        self,
        *,
        conn: psycopg.Connection,
        article: NewsArticle,
        result: LlmAnalysisResult,
        analysis_id: int,
        now: datetime,
        required_evidence_count: int,
        story_assignment_model: str | None,
        story_assignment_max_output_tokens: int | None,
        reprocess_run_id: str | None,
        instrument_display_name: str | None = None,
        instrument_aliases: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        candidates = self._load_story_candidates(
            conn=conn,
            result=result,
            now=now,
            reprocess_run_id=reprocess_run_id,
        )
        excluded_entity_tokens = _entity_exclusion_tokens(
            ticker=result.ticker,
            display_name=instrument_display_name,
            aliases=instrument_aliases,
        )
        assigner = self._story_assigner
        event_confirmer: Callable[[str], bool | None] | None = None
        if assigner is not None and getattr(assigner, "event_check_enabled", False):
            def event_confirmer(target_narrative: str) -> bool | None:
                return assigner.confirm_same_event(
                    article=article, analysis=result, narrative=target_narrative
                )
        candidate_targets = [candidate.target for candidate in candidates]
        assignment_source = "new_story"
        chosen_target = "new_story"
        resolved_target = "new_story"
        verification_status = "skipped"
        verification_reason_code: str | None = None
        verification_details: dict[str, Any] = {}
        llm_model = story_assignment_model or ""
        tokens_used = 0
        response_json: dict[str, Any] = {}
        error_code: str | None = None

        if candidates:
            try:
                if self._story_assigner is None:
                    raise RuntimeError("story_assignment_unavailable")
                assignment = self._story_assigner.assign_story(
                    article=article,
                    analysis=result,
                    candidates=candidates,
                )
                chosen_target = assignment.target
                assignment_source = "matched" if chosen_target != "new_story" else "new_story"
                llm_model = assignment.llm_model or llm_model
                tokens_used = assignment.estimated_tokens
                response_json = assignment.raw_response
            except Exception as exc:
                assignment_source = "fallback"
                error_code = str(exc) or exc.__class__.__name__
                fallback_window = self._load_oldest_collecting_window(
                    conn=conn,
                    result=result,
                    reprocess_run_id=reprocess_run_id,
                )
                if fallback_window is not None:
                    chosen_target = f"window:{fallback_window['id']}"
                    fallback_window["analyses"] = self._load_valid_analyses(
                        conn=conn,
                        analysis_ids=fallback_window["analysis_ids"],
                    )
                    verification = _verify_story_assignment_target(
                        article=article,
                        result=result,
                        target=chosen_target,
                        narrative=str(fallback_window.get("story_narrative") or ""),
                        excluded_entity_tokens=excluded_entity_tokens,
                        event_confirmer=event_confirmer,
                    )
                    resolved_target = verification["resolved_target"]
                    verification_status = verification["verification_status"]
                    verification_reason_code = verification["verification_reason_code"]
                    verification_details = verification["verification_details"]
                    self._insert_story_assignment(
                        conn=conn,
                        analysis_id=analysis_id,
                        article_id=article.id,
                        candidate_targets=candidate_targets,
                        chosen_target=chosen_target,
                        resolved_target=resolved_target,
                        assignment_source=assignment_source,
                        verification_status=verification_status,
                        verification_reason_code=verification_reason_code,
                        verification_details_json=verification_details,
                        llm_model=llm_model,
                        max_output_tokens=story_assignment_max_output_tokens,
                        tokens_used=tokens_used,
                        response_json=response_json,
                        error_code=error_code,
                        reprocess_run_id=reprocess_run_id,
                    )
                    if resolved_target == "new_story":
                        return {"target_type": "new_story", "target_id": None}
                    return {"target_type": "window", "target_id": fallback_window["id"], "window": fallback_window}
                chosen_target = "new_story"

        if chosen_target == "new_story":
            self._insert_story_assignment(
                conn=conn,
                analysis_id=analysis_id,
                article_id=article.id,
                candidate_targets=candidate_targets,
                chosen_target="new_story",
                resolved_target="new_story",
                assignment_source=assignment_source,
                verification_status="skipped",
                verification_reason_code=None,
                verification_details_json={},
                llm_model=llm_model,
                max_output_tokens=story_assignment_max_output_tokens,
                tokens_used=tokens_used,
                response_json=response_json,
                error_code=error_code,
                reprocess_run_id=reprocess_run_id,
            )
            return {"target_type": "new_story", "target_id": None}

        candidate_narratives = {candidate.target: candidate.narrative for candidate in candidates}
        verification = _verify_story_assignment_target(
            article=article,
            result=result,
            target=chosen_target,
            narrative=candidate_narratives.get(chosen_target, ""),
            excluded_entity_tokens=excluded_entity_tokens,
            event_confirmer=event_confirmer,
        )
        resolved_target = verification["resolved_target"]
        verification_status = verification["verification_status"]
        verification_reason_code = verification["verification_reason_code"]
        verification_details = verification["verification_details"]
        target_type, _, target_id_text = chosen_target.partition(":")
        self._insert_story_assignment(
            conn=conn,
            analysis_id=analysis_id,
            article_id=article.id,
            candidate_targets=candidate_targets,
            chosen_target=chosen_target,
            resolved_target=resolved_target,
            assignment_source=assignment_source,
            verification_status=verification_status,
            verification_reason_code=verification_reason_code,
            verification_details_json=verification_details,
            llm_model=llm_model,
            max_output_tokens=story_assignment_max_output_tokens,
            tokens_used=tokens_used,
            response_json=response_json,
            error_code=error_code,
            reprocess_run_id=reprocess_run_id,
        )
        if resolved_target == "new_story":
            return {"target_type": "new_story", "target_id": None}
        if target_type == "card":
            return {"target_type": "card", "target_id": target_id_text}
        if target_type == "window":
            window = self._load_window_by_id(conn=conn, window_id=int(target_id_text))
            if window is not None:
                window["analyses"] = self._load_valid_analyses(
                    conn=conn,
                    analysis_ids=window["analysis_ids"],
                )
                return {"target_type": "window", "target_id": window["id"], "window": window}
        raise ValueError("invalid_story_assignment_target")

    def _load_story_candidates(
        self,
        *,
        conn: psycopg.Connection,
        result: LlmAnalysisResult,
        now: datetime,
        reprocess_run_id: str | None,
    ) -> list[StoryAssignmentCandidate]:
        candidates: list[StoryAssignmentCandidate] = []
        sql = (
            f"SELECT id, story_narrative, event_identity_json FROM {self._thesis_schema}.t_evidence_windows "
            f"WHERE ticker = %s AND exchange_code = %s AND strategy = %s "
            f"AND COALESCE(direction, '') = COALESCE(%s, '') AND status = 'collecting' "
            f"AND COALESCE(reprocess_run_id, '') = COALESCE(%s, '') "
            f"ORDER BY window_started_at, id"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (result.ticker, result.exchange_code, result.candidate_strategy.value, result.direction.value, reprocess_run_id))
            for row in cur.fetchall():
                narrative = str(row["story_narrative"] or "").strip()
                if narrative:
                    identity = row.get("event_identity_json") or {}
                    if compare_event_identity(result.event_identity, identity) != "different":
                        candidates.append(StoryAssignmentCandidate(target=f"window:{row['id']}", narrative=narrative, event_identity=identity))
        card_sql = (
            f"SELECT id, story_narrative, event_identity_json FROM {self._thesis_schema}.t_thesis_cards "
            f"WHERE ticker = %s AND exchange_code = %s AND strategy = %s AND direction = %s "
            f"AND validation_status = 'valid' AND expires_at > %s "
            f"ORDER BY created_at, id"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(card_sql, (result.ticker, result.exchange_code, result.candidate_strategy.value, result.direction.value, _to_utc(now)))
            for row in cur.fetchall():
                narrative = str(row["story_narrative"] or "").strip()
                if narrative:
                    identity = row.get("event_identity_json") or {}
                    if compare_event_identity(result.event_identity, identity) != "different":
                        candidates.append(StoryAssignmentCandidate(target=f"card:{row['id']}", narrative=narrative, event_identity=identity))
        return candidates

    def _load_oldest_collecting_window(
        self,
        *,
        conn: psycopg.Connection,
        result: LlmAnalysisResult,
        reprocess_run_id: str | None,
    ) -> dict[str, Any] | None:
        sql = (
            f"SELECT id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count, story_narrative "
            f"FROM {self._thesis_schema}.t_evidence_windows "
            f"WHERE ticker = %s AND exchange_code = %s AND strategy = %s "
            f"AND COALESCE(direction, '') = COALESCE(%s, '') AND status = 'collecting' "
            f"AND COALESCE(reprocess_run_id, '') = COALESCE(%s, '') "
            f"ORDER BY window_started_at, id LIMIT 1"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (result.ticker, result.exchange_code, result.candidate_strategy.value, result.direction.value, reprocess_run_id))
            row = cur.fetchone()
        return _window(row) if row is not None else None

    def _load_window_by_id(self, *, conn: psycopg.Connection, window_id: int) -> dict[str, Any] | None:
        sql = (
            f"SELECT id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count, story_narrative "
            f"FROM {self._thesis_schema}.t_evidence_windows WHERE id = %s"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (window_id,))
            row = cur.fetchone()
        return _window(row) if row is not None else None

    def _create_story_window(
        self,
        *,
        conn: psycopg.Connection,
        result: LlmAnalysisResult,
        article: NewsArticle,
        analysis_id: int,
        required_evidence_count: int,
        reprocess_run_id: str | None,
    ) -> dict[str, Any]:
        story_narrative = _story_narrative(article=article, result=result)
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_evidence_windows "
            f"(ticker, exchange_code, strategy, direction, article_ids, analysis_ids, window_started_at, last_evidence_at, status, reprocess_run_id, required_evidence_count, story_narrative, event_identity_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'collecting', %s, %s, %s, %s) "
            f"RETURNING id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count, story_narrative"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                sql,
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
                    story_narrative,
                    Json(result.event_identity),
                ),
            )
            return _window(cur.fetchone())

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
            f"SELECT id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count, story_narrative "
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
                f"(ticker, exchange_code, strategy, direction, article_ids, analysis_ids, window_started_at, last_evidence_at, status, reprocess_run_id, required_evidence_count, event_identity_json) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'collecting', %s, %s, %s) RETURNING id, article_ids, analysis_ids, window_started_at, last_evidence_at, required_evidence_count, story_narrative"
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
                    Json(result.event_identity),
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
        window_started_at: datetime,
        last_evidence_at: datetime,
        status: str,
        status_reason: str | None,
    ) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_evidence_windows "
            f"SET article_ids = %s, analysis_ids = %s, window_started_at = %s, last_evidence_at = %s, status = %s, status_reason = %s, updated_at = NOW() "
            f"WHERE id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (Json(article_ids), Json(analysis_ids), _to_utc(window_started_at), _to_utc(last_evidence_at), status, status_reason, window_id))

    def _reject_analysis(
        self,
        *,
        conn: psycopg.Connection,
        analysis_id: int,
        rejection_reason_code: str,
    ) -> None:
        sql = (
            f"UPDATE {self._thesis_schema}.t_news_analyses "
            f"SET validation_status = 'rejected', "
            f"rejection_reason_code = %s, "
            f"validation_errors = COALESCE(validation_errors, '[]'::jsonb) || %s::jsonb "
            f"WHERE id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (rejection_reason_code, Json([rejection_reason_code]), analysis_id))

    def _load_valid_analyses(self, *, conn: psycopg.Connection, analysis_ids: list[int]) -> list[PersistedAnalysis]:
        sql = (
            f"SELECT id, article_id, article_snapshot, ticker, exchange_code, strategy, direction, confidence, reasoning, "
            f"validation_status, rejection_reason_code, subject_relation, event_occurred_at, analyzed_at "
            f"FROM {self._thesis_schema}.t_news_analyses "
            f"WHERE id = ANY(%s) AND validation_status = 'valid' "
            f"ORDER BY analyzed_at, id"
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (analysis_ids,))
            rows = cur.fetchall()
        return [_analysis(row) for row in rows]

    def _insert_card(self, *, conn: psycopg.Connection, card_id: str, idempotency_key: str, result: LlmAnalysisResult, evidence: list[dict[str, Any]], source_analysis_ids: list[int], confidence: float, risk_box: dict[str, Any], market_context_snapshot: dict[str, Any] | None, validation_status: ValidationStatus, rejection_reason_code: str | None, max_evidence_age_seconds: float, allowed_max_evidence_age_seconds: float, evidence_age_exceeded_seconds: float, expires_at: datetime, created_at: datetime, default_time_horizon: str, story_narrative: str | None = None) -> bool:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_thesis_cards "
            f"(id, idempotency_key, ticker, exchange_code, direction, time_horizon, strategy, evidence, source_analysis_ids, confidence, "
            f"risk_max_loss_usd, risk_stop_condition, risk_invalidation_condition, market_context_status, market_context_as_of, market_context_snapshot, "
            f"validation_status, validation_errors, rejection_reason_code, max_evidence_age_seconds, allowed_max_evidence_age_seconds, evidence_age_exceeded_seconds, expires_at, created_at, story_narrative, event_identity_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
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
                    story_narrative,
                    Json(result.event_identity),
                ),
            )
            return cur.rowcount == 1

    def _insert_synthesis_verdict(
        self,
        *,
        conn: psycopg.Connection,
        evidence_window_id: int,
        card_id: str | None,
        result: LlmAnalysisResult,
        verdict: str,
        reason_code: str | None,
        confidence: float | None,
        llm_model: str | None,
        max_output_tokens: int | None,
        response_json: dict[str, Any],
    ) -> None:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_card_synthesis_verdicts "
            f"(evidence_window_id, card_id, ticker, exchange_code, strategy, direction, verdict, "
            f"reason_code, confidence, llm_model, max_output_tokens, response_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    evidence_window_id,
                    card_id,
                    result.ticker,
                    result.exchange_code,
                    result.candidate_strategy.value,
                    result.direction.value,
                    verdict,
                    reason_code,
                    confidence,
                    llm_model or "",
                    max_output_tokens,
                    Json(response_json),
                ),
            )

    def _insert_card_corroboration(
        self,
        *,
        conn: psycopg.Connection,
        card_id: str,
        article_id: str,
        analysis_id: int,
        matched_at: datetime,
    ) -> None:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_card_corroborations "
            f"(card_id, article_id, analysis_id, matched_at) "
            f"VALUES (%s, %s, %s, %s) "
            f"ON CONFLICT (card_id, article_id) DO NOTHING"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (card_id, article_id, analysis_id, _to_utc(matched_at)))

    def _insert_story_assignment(
        self,
        *,
        conn: psycopg.Connection,
        analysis_id: int,
        article_id: str,
        candidate_targets: list[str],
        chosen_target: str,
        resolved_target: str,
        assignment_source: str,
        verification_status: str,
        verification_reason_code: str | None,
        verification_details_json: dict[str, Any],
        llm_model: str,
        max_output_tokens: int | None,
        tokens_used: int,
        response_json: dict[str, Any],
        error_code: str | None,
        reprocess_run_id: str | None,
    ) -> None:
        sql = (
            f"INSERT INTO {self._thesis_schema}.t_story_assignments "
            f"(analysis_id, article_id, candidate_targets, chosen_target, resolved_target, "
            f"assignment_source, verification_status, verification_reason_code, verification_details_json, "
            f"llm_model, max_output_tokens, tokens_used, response_json, error_code, reprocess_run_id) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (analysis_id) DO UPDATE SET "
            f"candidate_targets = EXCLUDED.candidate_targets, "
            f"chosen_target = EXCLUDED.chosen_target, "
            f"resolved_target = EXCLUDED.resolved_target, "
            f"assignment_source = EXCLUDED.assignment_source, "
            f"verification_status = EXCLUDED.verification_status, "
            f"verification_reason_code = EXCLUDED.verification_reason_code, "
            f"verification_details_json = EXCLUDED.verification_details_json, "
            f"llm_model = EXCLUDED.llm_model, "
            f"max_output_tokens = EXCLUDED.max_output_tokens, "
            f"tokens_used = EXCLUDED.tokens_used, "
            f"response_json = EXCLUDED.response_json, "
            f"error_code = EXCLUDED.error_code"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    analysis_id,
                    article_id,
                    Json(candidate_targets),
                    chosen_target,
                    resolved_target,
                    assignment_source,
                    verification_status,
                    verification_reason_code,
                    Json(verification_details_json),
                    llm_model,
                    max_output_tokens,
                    tokens_used,
                    Json(response_json),
                    error_code,
                    reprocess_run_id,
                ),
            )

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

    def get_taxonomy_revision(self) -> int:
        sql = (
            f"SELECT taxonomy_revision FROM {self._thesis_schema}."
            "t_event_taxonomy_state WHERE singleton = TRUE"
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("taxonomy_state_missing")
        return int(row["taxonomy_revision"])

    def load_taxonomy_values(
        self, *, taxonomy_revision: int
    ) -> tuple[TaxonomyValue, ...]:
        if taxonomy_revision <= 0:
            raise ValueError("invalid_taxonomy_revision")
        sql = (
            f"SELECT dimension, canonical_value, status, family_rules, alias_for_value "
            f"FROM {self._thesis_schema}.t_event_taxonomy_values "
            "WHERE effective_from_revision <= %s "
            "AND (effective_to_revision IS NULL OR effective_to_revision > %s) "
            "AND status IN ('active', 'mapped_alias') "
            "ORDER BY dimension, canonical_value"
        )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT taxonomy_revision FROM {self._thesis_schema}."
                    "t_event_taxonomy_state WHERE singleton = TRUE"
                )
                state = cur.fetchone()
                if (
                    state is None
                    or taxonomy_revision > int(state["taxonomy_revision"])
                ):
                    raise ValueError("taxonomy_revision_unavailable")
                cur.execute(sql, (taxonomy_revision, taxonomy_revision))
                rows = cur.fetchall()
        return tuple(
            TaxonomyValue(
                dimension=str(row["dimension"]),
                canonical_value=str(row["canonical_value"]),
                status=str(row["status"]),
                family_scope=family_rules_scope(row.get("family_rules")),
                alias_for_value=(
                    str(row["alias_for_value"])
                    if row.get("alias_for_value")
                    else None
                ),
            )
            for row in rows
        )

    def decide_taxonomy_gap(self, *, request: TaxonomyDecisionRequest, actor: str) -> TaxonomyDecisionRecord:
        """Atomically resolve one open proposal and advance the integer revision once.

        This is deliberately a ThesisBuilder repository operation: callers never
        receive SQL-level access to the taxonomy tables.
        """
        with self._connect() as conn:
            try:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"SELECT id, gap_id, action, taxonomy_revision FROM {self._thesis_schema}.t_event_taxonomy_decisions WHERE idempotency_key = %s",
                        (request.idempotency_key,),
                    )
                    existing = cur.fetchone()
                    if existing:
                        return TaxonomyDecisionRecord(command_id=str(existing["id"]), gap_id=int(existing["gap_id"]), action=str(existing["action"]), status="completed", taxonomy_revision=int(existing["taxonomy_revision"]))
                    cur.execute(
                        f"SELECT id, dimension, normalized_proposal, status FROM {self._thesis_schema}.t_event_taxonomy_gaps WHERE id = %s FOR UPDATE",
                        (request.gap_id,),
                    )
                    gap = cur.fetchone()
                    if gap is None or gap["status"] != request.expected_gap_status:
                        raise TaxonomyDecisionValidationError("taxonomy_gap_conflict")
                    dimension = str(gap["dimension"])
                    request.validate(dimension=dimension)
                    if request.action == "map_existing":
                        cur.execute(
                            f"SELECT canonical_value FROM {self._thesis_schema}.t_event_taxonomy_values WHERE dimension = %s AND canonical_value = %s AND status = 'active' AND effective_to_revision IS NULL AND (%s <> 'event_subtype' OR family_rules->>'family' = %s)",
                            (dimension, request.canonical_value, dimension, request.family_scope),
                        )
                        if cur.fetchone() is None:
                            raise TaxonomyDecisionValidationError("invalid_mapping_target")
                    elif request.action == "accept_new":
                        cur.execute(
                            f"SELECT 1 FROM {self._thesis_schema}.t_event_taxonomy_values WHERE dimension = %s AND canonical_value = %s AND effective_to_revision IS NULL",
                            (dimension, request.canonical_value),
                        )
                        if cur.fetchone():
                            raise TaxonomyDecisionValidationError("canonical_value_collision")
                    cur.execute(f"SELECT taxonomy_revision FROM {self._thesis_schema}.t_event_taxonomy_state WHERE singleton = TRUE FOR UPDATE")
                    revision = int(cur.fetchone()["taxonomy_revision"]) + 1
                    cur.execute(f"UPDATE {self._thesis_schema}.t_event_taxonomy_state SET taxonomy_revision = %s WHERE singleton = TRUE", (revision,))
                    new_value = {"canonical_value": request.canonical_value, "display_name": request.display_name, "description": request.description, "family_scope": request.family_scope, "identity_discriminators": list(request.identity_discriminators)}
                    cur.execute(
                        f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_decisions (gap_id, action, actor, old_value, new_value, rationale, idempotency_key, taxonomy_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (request.gap_id, request.action, actor[:120], Json({"proposal": gap["normalized_proposal"]}), Json(new_value), request.rationale.strip(), request.idempotency_key, revision),
                    )
                    decision_id = int(cur.fetchone()["id"])
                    if request.action == "map_existing":
                        cur.execute(
                            f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_values (dimension, canonical_value, display_name, description, status, taxonomy_version, family_rules, alias_for_value, effective_from_revision) VALUES (%s, %s, %s, %s, 'mapped_alias', 'event-taxonomy-v1', %s, %s, %s)",
                            (dimension, str(gap["normalized_proposal"]), str(gap["normalized_proposal"]), "Operator-approved alias", Json({"family": request.family_scope} if request.family_scope else {}), request.canonical_value, revision),
                        )
                    elif request.action == "accept_new":
                        cur.execute(
                            f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_values (dimension, canonical_value, display_name, description, status, taxonomy_version, family_rules, effective_from_revision) VALUES (%s, %s, %s, %s, 'active', 'event-taxonomy-v1', %s, %s)",
                            (dimension, request.canonical_value, request.display_name, request.description, Json({"family": request.family_scope, "identity_discriminators": list(request.identity_discriminators)}), revision),
                        )
                    status = {"map_existing": "mapped", "accept_new": "accepted", "reject": "rejected"}[request.action]
                    cur.execute(f"UPDATE {self._thesis_schema}.t_event_taxonomy_gaps SET status = %s, resolution = %s WHERE id = %s", (status, Json({"decision_id": decision_id, "taxonomy_revision": revision, "canonical_value": request.canonical_value}), request.gap_id))
                    cur.execute(
                        f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                        "(decision_id, requested_taxonomy_revision, taxonomy_revision) "
                        "VALUES (%s, %s, %s)",
                        (decision_id, revision - 1, revision),
                    )
                conn.commit()
                return TaxonomyDecisionRecord(command_id=str(decision_id), gap_id=request.gap_id, action=request.action, status="completed", taxonomy_revision=revision)
            except Exception:
                conn.rollback()
                raise

    def submit_taxonomy_command(
        self,
        *,
        command_id: str,
        request: TaxonomyDecisionRequest,
        actor: str,
    ) -> tuple[TaxonomyCommand, bool]:
        request_json = {
            "gap_id": request.gap_id,
            "expected_gap_status": request.expected_gap_status,
            "action": request.action,
            "canonical_value": request.canonical_value,
            "display_name": request.display_name,
            "description": request.description,
            "family_scope": request.family_scope,
            "identity_discriminators": list(request.identity_discriminators),
            "rationale": request.rationale,
            "idempotency_key": request.idempotency_key,
        }
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT request_json FROM {self._thesis_schema}.t_event_taxonomy_commands "
                    "WHERE idempotency_key = %s",
                    (request.idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if dict(existing["request_json"]) != request_json:
                        raise TaxonomyDecisionValidationError("idempotency_key_conflict")
                    conn.commit()
                    command = self.get_taxonomy_command_by_idempotency_key(
                        idempotency_key=request.idempotency_key
                    )
                    assert command is not None
                    return command, False
                cur.execute(
                    f"INSERT INTO {self._thesis_schema}.t_event_taxonomy_commands "
                    "(command_id, gap_id, action, request_json, idempotency_key, actor) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        command_id,
                        request.gap_id,
                        request.action,
                        Json(request_json),
                        request.idempotency_key,
                        actor,
                    ),
                )
            conn.commit()
        command = self.get_taxonomy_command(command_id=command_id)
        assert command is not None
        return command, True

    def mark_taxonomy_command_publish_failed(self, *, command_id: str) -> None:
        # Keep the command accepted. Runtime DB recovery is the durable fallback
        # when Redis is temporarily unavailable.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_commands "
                    "SET error_code = 'command_publish_failed', updated_at = NOW() "
                    "WHERE command_id = %s AND status = 'accepted'",
                    (command_id,),
                )
            conn.commit()

    def get_taxonomy_command_by_idempotency_key(
        self, *, idempotency_key: str
    ) -> TaxonomyCommand | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    self._taxonomy_command_status_sql()
                    + " WHERE c.idempotency_key = %s",
                    (idempotency_key,),
                )
                row = cur.fetchone()
        return _taxonomy_command(row) if row else None

    def get_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    self._taxonomy_command_status_sql()
                    + " WHERE c.command_id = %s",
                    (command_id,),
                )
                row = cur.fetchone()
        return _taxonomy_command(row) if row else None

    def claim_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_commands "
                    "SET status = 'running', started_at = COALESCE(started_at, NOW()), "
                    "updated_at = NOW(), error_code = NULL "
                    "WHERE command_id = %s AND status = 'accepted' RETURNING command_id",
                    (command_id,),
                )
                claimed = cur.fetchone()
            conn.commit()
        if claimed is None:
            return None
        return self.get_taxonomy_command(command_id=command_id)

    def execute_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT request_json, actor, status FROM {self._thesis_schema}.t_event_taxonomy_commands "
                    "WHERE command_id = %s",
                    (command_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise TaxonomyDecisionValidationError("taxonomy_command_not_found")
        if row["status"] == "completed":
            command = self.get_taxonomy_command(command_id=command_id)
            assert command is not None
            return command
        if row["status"] != "running":
            raise TaxonomyDecisionValidationError("taxonomy_command_not_running")
        request = _taxonomy_request(dict(row["request_json"]))
        decision = self.decide_taxonomy_gap(request=request, actor=str(row["actor"]))
        try:
            with self._connect() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"SELECT id FROM {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                        "WHERE decision_id = %s",
                        (int(decision.command_id),),
                    )
                    backfill_row = cur.fetchone()
                    backfill_id = int(backfill_row["id"]) if backfill_row else None
                    cur.execute(
                        f"UPDATE {self._thesis_schema}.t_event_taxonomy_commands "
                        "SET status = 'completed', result_taxonomy_revision = %s, decision_id = %s, "
                        "backfill_job_id = %s, error_code = NULL, updated_at = NOW(), finished_at = NOW() "
                        "WHERE command_id = %s",
                        (
                            decision.taxonomy_revision,
                            int(decision.command_id),
                            backfill_id,
                            command_id,
                        ),
                    )
                conn.commit()
        except Exception as exc:
            raise TaxonomyCommandRetryableError(
                "taxonomy_command_reconciliation_pending"
            ) from exc
        command = self.get_taxonomy_command(command_id=command_id)
        assert command is not None
        return command

    def fail_taxonomy_command(self, *, command_id: str, error_code: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_commands "
                    "SET status = 'failed', error_code = %s, updated_at = NOW(), finished_at = NOW() "
                    "WHERE command_id = %s AND status = 'running'",
                    (error_code[:80], command_id),
                )
            conn.commit()

    def recoverable_taxonomy_command_ids(self, *, limit: int) -> list[str]:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_commands "
                    "SET status = 'accepted', error_code = 'recovered_after_restart', updated_at = NOW() "
                    "WHERE status = 'running' AND updated_at < NOW() - INTERVAL '5 minutes'"
                )
                cur.execute(
                    f"SELECT command_id FROM {self._thesis_schema}.t_event_taxonomy_commands "
                    "WHERE status = 'accepted' ORDER BY requested_at LIMIT %s",
                    (max(1, min(limit, 100)),),
                )
                rows = cur.fetchall()
            conn.commit()
        return [str(row["command_id"]) for row in rows]

    def claim_taxonomy_backfill_job(self) -> TaxonomyBackfillJob | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                    "SET status = 'accepted', retry_count = retry_count + 1, "
                    "error_code = 'recovered_after_restart', updated_at = NOW() "
                    "WHERE status = 'running' AND updated_at < NOW() - INTERVAL '5 minutes'"
                )
                cur.execute(
                    f"WITH candidate AS ("
                    f"SELECT j.id FROM {self._thesis_schema}.t_event_taxonomy_backfill_jobs j "
                    "WHERE j.status = 'accepted' ORDER BY j.created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_backfill_jobs j "
                    "SET status = 'running', started_at = COALESCE(started_at, NOW()), updated_at = NOW(), error_code = NULL "
                    "FROM candidate WHERE j.id = candidate.id RETURNING j.id, j.decision_id, "
                    "j.requested_taxonomy_revision, j.taxonomy_revision, j.last_analysis_id"
                )
                job_row = cur.fetchone()
                if job_row is None:
                    conn.commit()
                    return None
                cur.execute(
                    f"SELECT g.dimension, g.normalized_proposal "
                    f"FROM {self._thesis_schema}.t_event_taxonomy_decisions d "
                    f"JOIN {self._thesis_schema}.t_event_taxonomy_gaps g ON g.id = d.gap_id "
                    "WHERE d.id = %s",
                    (job_row["decision_id"],),
                )
                gap = cur.fetchone()
            conn.commit()
        assert gap is not None
        return TaxonomyBackfillJob(
            job_id=int(job_row["id"]),
            decision_id=int(job_row["decision_id"]),
            dimension=str(gap["dimension"]),
            proposal=str(gap["normalized_proposal"]),
            requested_taxonomy_revision=int(job_row["requested_taxonomy_revision"]),
            target_taxonomy_revision=int(job_row["taxonomy_revision"]),
            last_analysis_id=int(job_row["last_analysis_id"]),
        )

    def get_taxonomy_backfill_batch(
        self, *, job: TaxonomyBackfillJob, batch_size: int
    ) -> list[TaxonomyBackfillAnalysis]:
        candidate_key = f"{job.dimension}_candidate"
        if job.dimension == "participant_role":
            # Participant-role candidates are arrays and need a containment query.
            match_sql = (
                "EXISTS (SELECT 1 FROM jsonb_array_elements("
                "COALESCE(event_identity_json->'participants', '[]'::jsonb)"
                ") participant WHERE participant->>'role_candidate' = %s)"
            )
        else:
            match_sql = "event_identity_json->>%s = %s"
        params: tuple[Any, ...]
        if job.dimension == "participant_role":
            params = (job.last_analysis_id, job.proposal, max(1, min(batch_size, 1000)))
        else:
            params = (
                job.last_analysis_id,
                candidate_key,
                job.proposal,
                max(1, min(batch_size, 1000)),
            )
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT id, event_identity_json FROM {self._thesis_schema}.t_news_analyses "
                    f"WHERE id > %s AND {match_sql} ORDER BY id LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
        return [
            TaxonomyBackfillAnalysis(
                analysis_id=int(row["id"]),
                event_identity=dict(row["event_identity_json"] or {}),
            )
            for row in rows
        ]

    def persist_taxonomy_backfill_batch(
        self,
        *,
        job: TaxonomyBackfillJob,
        rows: list[tuple[int, dict[str, Any], bool]],
        failed_count: int,
        complete: bool,
    ) -> None:
        last_analysis_id = max((row[0] for row in rows), default=job.last_analysis_id)
        changed_count = sum(1 for _, _, changed in rows if changed)
        skipped_count = len(rows) - changed_count
        with self._connect() as conn:
            with conn.cursor() as cur:
                for analysis_id, identity, changed in rows:
                    if changed:
                        cur.execute(
                            f"UPDATE {self._thesis_schema}.t_news_analyses "
                            "SET event_identity_json = %s, taxonomy_revision = %s "
                            "WHERE id = %s",
                            (Json(identity), job.target_taxonomy_revision, analysis_id),
                        )
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                    "SET status = %s, last_analysis_id = %s, "
                    "matched_count = matched_count + %s, processed_count = processed_count + %s, "
                    "changed_count = changed_count + %s, skipped_count = skipped_count + %s, "
                    "failed_count = failed_count + %s, affected_rows = affected_rows + %s, "
                    "updated_at = NOW(), finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END "
                    "WHERE id = %s AND status = 'running'",
                    (
                        "completed" if complete else "accepted",
                        last_analysis_id,
                        len(rows) + failed_count,
                        len(rows) - failed_count,
                        changed_count,
                        skipped_count,
                        failed_count,
                        changed_count,
                        complete,
                        job.job_id,
                    ),
                )
            conn.commit()

    def fail_taxonomy_backfill_job(self, *, job_id: int, error_code: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                    "SET status = 'failed', retry_count = retry_count + 1, error_code = %s, "
                    "updated_at = NOW(), finished_at = NOW() WHERE id = %s",
                    (error_code[:80], job_id),
                )
            conn.commit()

    def retry_taxonomy_backfill_job(self, *, job_id: int) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._thesis_schema}.t_event_taxonomy_backfill_jobs "
                    "SET status = 'accepted', error_code = NULL, finished_at = NULL, updated_at = NOW() "
                    "WHERE id = %s AND status = 'failed'",
                    (job_id,),
                )
                changed = cur.rowcount == 1
            conn.commit()
        return changed

    def _taxonomy_command_status_sql(self) -> str:
        return (
            "SELECT c.command_id, c.gap_id, c.action, c.status, "
            "c.result_taxonomy_revision, c.error_code, c.requested_at, c.started_at, c.finished_at, "
            "j.id AS job_id, j.status AS job_status, j.requested_taxonomy_revision, "
            "j.taxonomy_revision AS target_taxonomy_revision, j.last_analysis_id, "
            "j.matched_count, j.processed_count, j.changed_count, j.skipped_count, "
            "j.failed_count, j.retry_count, j.error_code AS job_error_code, "
            "j.started_at AS job_started_at, j.updated_at AS job_updated_at, "
            "j.finished_at AS job_finished_at "
            f"FROM {self._thesis_schema}.t_event_taxonomy_commands c "
            f"LEFT JOIN {self._thesis_schema}.t_event_taxonomy_backfill_jobs j "
            "ON j.id = c.backfill_job_id"
        )

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


def _taxonomy_request(payload: dict[str, Any]) -> TaxonomyDecisionRequest:
    return TaxonomyDecisionRequest(
        gap_id=int(payload["gap_id"]),
        expected_gap_status=str(payload["expected_gap_status"]),
        action=str(payload["action"]),
        canonical_value=payload.get("canonical_value"),
        display_name=payload.get("display_name"),
        description=payload.get("description"),
        family_scope=payload.get("family_scope"),
        identity_discriminators=tuple(payload.get("identity_discriminators") or ()),
        rationale=str(payload["rationale"]),
        idempotency_key=str(payload["idempotency_key"]),
    )


def _taxonomy_command(row: dict[str, Any]) -> TaxonomyCommand:
    backfill = None
    if row["job_id"] is not None:
        backfill = TaxonomyBackfillStatus(
            job_id=int(row["job_id"]),
            status=str(row["job_status"]),
            requested_taxonomy_revision=int(row["requested_taxonomy_revision"]),
            target_taxonomy_revision=int(row["target_taxonomy_revision"]),
            last_analysis_id=int(row["last_analysis_id"]),
            matched_count=int(row["matched_count"]),
            processed_count=int(row["processed_count"]),
            changed_count=int(row["changed_count"]),
            skipped_count=int(row["skipped_count"]),
            failed_count=int(row["failed_count"]),
            retry_count=int(row["retry_count"]),
            error_code=row["job_error_code"],
            started_at=row["job_started_at"],
            updated_at=row["job_updated_at"],
            finished_at=row["job_finished_at"],
        )
    return TaxonomyCommand(
        command_id=str(row["command_id"]),
        gap_id=int(row["gap_id"]),
        action=str(row["action"]),
        status=str(row["status"]),
        taxonomy_revision=(
            int(row["result_taxonomy_revision"])
            if row["result_taxonomy_revision"] is not None
            else None
        ),
        error_code=row["error_code"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        backfill=backfill,
    )


def _analysis_rejection(
    result: LlmAnalysisResult,
    *,
    min_confidence: float,
    min_relevance: float = 0.0,
    has_anchor_evidence: bool = False,
) -> str | None:
    # Discard articles that are not genuinely about this instrument first, so incidental
    # name-drops (e.g. a comparison mention in another company's story) are dropped as noise
    # rather than retained for the analyst.
    relation = _effective_subject_relation(result)
    if relation is SubjectRelation.MACRO_SECTOR:
        return "macro_sector_not_subject"
    if relation is SubjectRelation.NONE:
        return "instrument_not_subject"
    if relation in {SubjectRelation.SUPPLY_CHAIN, SubjectRelation.CUSTOMER_OR_PEER}:
        if not has_anchor_evidence:
            return "indirect_no_anchor_evidence"
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


def _normalize_analysis_result(
    result: LlmAnalysisResult,
    *,
    article: NewsArticle | None = None,
    instrument_display_name: str | None = None,
    instrument_aliases: tuple[str, ...] = (),
) -> LlmAnalysisResult:
    relation = _effective_subject_relation(result)
    if (
        relation is SubjectRelation.DIRECT
        and article is not None
        and not _article_text_names_instrument(
            article=article,
            ticker=result.ticker,
            display_name=instrument_display_name,
            aliases=instrument_aliases,
        )
    ):
        relation = SubjectRelation.CUSTOMER_OR_PEER
    normalized = result
    if result.subject_relation is not relation or result.instrument_is_subject is not (
        relation is SubjectRelation.DIRECT
    ):
        normalized = replace(
            normalized,
            subject_relation=relation,
            instrument_is_subject=relation is SubjectRelation.DIRECT,
        )
    if (
        relation is not SubjectRelation.DIRECT
        and normalized.price_impact_magnitude in {"medium", "high"}
        and not _reports_realized_surprise(normalized)
    ):
        normalized = replace(normalized, price_impact_magnitude="low")
    return normalized


def _normalization_validation_errors(
    original: LlmAnalysisResult,
    normalized: LlmAnalysisResult,
) -> list[str]:
    if (
        _effective_subject_relation(original) is SubjectRelation.DIRECT
        and normalized.subject_relation is SubjectRelation.CUSTOMER_OR_PEER
        and not normalized.instrument_is_subject
    ):
        return [_DIRECT_TEXT_DOWNGRADE_AUDIT]
    return []


def _effective_subject_relation(result: LlmAnalysisResult) -> SubjectRelation:
    if result.subject_relation is SubjectRelation.NONE and result.instrument_is_subject:
        return SubjectRelation.DIRECT
    return result.subject_relation


def _article_text_names_instrument(
    *,
    article: NewsArticle,
    ticker: str,
    display_name: str | None,
    aliases: tuple[str, ...],
) -> bool:
    text = " ".join(part for part in (article.headline, article.summary or "") if part)
    terms = [ticker, *(alias for alias in aliases if alias), display_name or ""]
    return any(contains_term(text, term) for term in terms)


def _is_direct_relation(relation: SubjectRelation | None) -> bool:
    return relation is None or relation is SubjectRelation.DIRECT


def _reports_realized_surprise(result: LlmAnalysisResult) -> bool:
    text = f"{result.event_type or ''} {result.reasoning}".lower()
    surprise_terms = ("beat", "miss", "surprise", "actual", "reported", "results")
    preview_terms = ("preview", "expected", "consensus", "forecast", "seen")
    return any(term in text for term in surprise_terms) and not any(
        term in text for term in preview_terms
    )


def _has_anchor_evidence(
    *,
    conn: psycopg.Connection,
    schema: str,
    result: LlmAnalysisResult,
    reprocess_run_id: str | None,
    now: datetime,
) -> bool:
    relation = _effective_subject_relation(result)
    if relation is SubjectRelation.DIRECT:
        return True
    if relation not in {SubjectRelation.SUPPLY_CHAIN, SubjectRelation.CUSTOMER_OR_PEER}:
        return False
    active_card_sql = (
        f"SELECT 1 FROM {schema}.t_thesis_cards "
        f"WHERE ticker = %s AND exchange_code = %s "
        f"AND validation_status = 'valid' AND expires_at > %s LIMIT 1"
    )
    direct_window_sql = (
        f"SELECT 1 FROM {schema}.t_evidence_windows w "
        f"JOIN LATERAL jsonb_array_elements_text(w.analysis_ids) AS ids(id_text) ON TRUE "
        f"JOIN {schema}.t_news_analyses a ON a.id = ids.id_text::bigint "
        f"WHERE w.ticker = %s AND w.exchange_code = %s AND w.strategy = %s "
        f"AND COALESCE(w.direction, '') = COALESCE(%s, '') "
        f"AND w.status = 'collecting' "
        f"AND COALESCE(w.reprocess_run_id, '') = COALESCE(%s, '') "
        f"AND a.validation_status = 'valid' "
        f"AND COALESCE(a.subject_relation, 'direct') = 'direct' "
        f"LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(active_card_sql, (result.ticker, result.exchange_code, _to_utc(now)))
        if cur.fetchone() is not None:
            return True
        cur.execute(
            direct_window_sql,
            (
                result.ticker,
                result.exchange_code,
                result.candidate_strategy.value,
                result.direction.value,
                reprocess_run_id,
            ),
        )
        return cur.fetchone() is not None


def _target_has_anchor_evidence(*, target: dict[str, Any], result: LlmAnalysisResult) -> bool:
    relation = _effective_subject_relation(result)
    if relation is SubjectRelation.DIRECT:
        return True
    if relation not in {SubjectRelation.SUPPLY_CHAIN, SubjectRelation.CUSTOMER_OR_PEER}:
        return False
    if target["target_type"] == "card":
        return True
    if target["target_type"] != "window":
        return False
    window = target.get("window") or {}
    analyses = window.get("analyses") or []
    return any(_is_direct_relation(analysis.subject_relation) for analysis in analyses)


def _entity_exclusion_tokens(
    *,
    ticker: str,
    display_name: str | None,
    aliases: tuple[str, ...],
) -> frozenset[str]:
    """Tokens naming the subject instrument itself.

    Two articles about the same watchlist company always share the company name, so name
    tokens carry no story-identity signal and must be excluded from the overlap check
    (the ticker symbol alone is not enough — the press uses the company name, not "NVDA").
    """
    tokens = {ticker.lower()}
    for name in (display_name or "", *aliases):
        for token in re.findall(r"[a-z0-9]+", name.lower()):
            if len(token) >= 2:
                tokens.add(token)
    return frozenset(tokens)


def _verify_story_assignment_target(
    *,
    article: NewsArticle,
    result: LlmAnalysisResult,
    target: str,
    narrative: str,
    excluded_entity_tokens: frozenset[str] = frozenset(),
    event_confirmer: Callable[[str], bool | None] | None = None,
) -> dict[str, Any]:
    if target == "new_story":
        return _story_verification(
            resolved_target="new_story",
            verification_status="skipped",
            verification_reason_code=None,
            incoming_tokens=[],
            target_tokens=[],
            overlap=[],
        )
    excluded_tokens = {result.ticker.lower(), *excluded_entity_tokens}
    incoming_tokens = _story_tokens(
        " ".join(
            [
                article.headline,
                article.summary or "",
                result.event_type or "",
                " ".join(result.evidence_bullet_candidates),
            ]
        ),
        excluded_tokens=excluded_tokens,
    )
    target_tokens = _story_tokens(narrative, excluded_tokens=excluded_tokens)
    overlap = sorted(incoming_tokens & target_tokens)
    inc_sorted = sorted(incoming_tokens)
    tgt_sorted = sorted(target_tokens)

    # Two or more distinctive tokens in common: strong same-story signal; accept deterministically.
    if len(overlap) >= 2:
        return _story_verification(
            resolved_target=target,
            verification_status="passed",
            verification_reason_code=None,
            incoming_tokens=inc_sorted,
            target_tokens=tgt_sorted,
            overlap=overlap,
        )

    # Zero or one distinctive shared token is lexically inconclusive. Zero overlap cannot prove
    # different-event identity: paraphrased coverage of the same announcement can retain no
    # common tokens once subject and generic-domain terms are removed (260722-01). A single
    # token is likewise often incidental (260716-01). Consult the event check when available.
    if event_confirmer is not None:
        decision = event_confirmer(narrative)
        if decision is False:
            return _story_verification(
                resolved_target="new_story",
                verification_status="downgraded",
                verification_reason_code="story_event_mismatch",
                incoming_tokens=inc_sorted,
                target_tokens=tgt_sorted,
                overlap=overlap,
                event_check="different",
            )
        if decision is True:
            return _story_verification(
                resolved_target=target,
                verification_status="passed",
                verification_reason_code=None,
                incoming_tokens=inc_sorted,
                target_tokens=tgt_sorted,
                overlap=overlap,
                event_check="same",
            )

    # No event decision (check disabled, unsupported, budget-exhausted, or transport error).
    # A one-token match retains the pre-260716-01 fail-open behavior; a zero-token match has no
    # lexical affirmative evidence, so preserve the safe new-story fallback with auditable cause.
    if not overlap:
        return _story_verification(
            resolved_target="new_story",
            verification_status="downgraded",
            verification_reason_code="story_event_check_unavailable",
            incoming_tokens=inc_sorted,
            target_tokens=tgt_sorted,
            overlap=overlap,
            event_check="unavailable",
        )

    return _story_verification(
        resolved_target=target,
        verification_status="passed",
        verification_reason_code=None,
        incoming_tokens=inc_sorted,
        target_tokens=tgt_sorted,
        overlap=overlap,
    )


def _story_tokens(text: str, *, excluded_tokens: set[str]) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", text.lower()):
        normalized = token.strip("-")
        if len(normalized) < 4:
            continue
        if normalized.isdigit():
            continue
        if normalized in excluded_tokens or normalized in _STORY_GENERIC_TOKENS:
            continue
        tokens.add(normalized)
    return tokens


def _story_verification(
    *,
    resolved_target: str,
    verification_status: str,
    verification_reason_code: str | None,
    incoming_tokens: list[str],
    target_tokens: list[str],
    overlap: list[str],
    event_check: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "incoming_tokens": incoming_tokens[:20],
        "target_tokens": target_tokens[:20],
        "overlap": overlap[:20],
    }
    if event_check is not None:
        details["event_check"] = event_check
    return {
        "resolved_target": resolved_target,
        "verification_status": verification_status,
        "verification_reason_code": verification_reason_code,
        "verification_details": details,
    }


def _already_priced_rejection(
    *,
    result: LlmAnalysisResult,
    market_context_snapshot: dict[str, Any] | None,
    event_driven_atr_multiple: float,
    event_driven_return_threshold: float,
    sentiment_momentum_atr_multiple: float,
    sentiment_momentum_return_threshold: float,
) -> str | None:
    thresholds = _already_priced_thresholds(
        result.candidate_strategy,
        event_driven_atr_multiple=event_driven_atr_multiple,
        event_driven_return_threshold=event_driven_return_threshold,
        sentiment_momentum_atr_multiple=sentiment_momentum_atr_multiple,
        sentiment_momentum_return_threshold=sentiment_momentum_return_threshold,
    )
    if thresholds is None:
        return None
    atr_multiple, return_threshold = thresholds
    metrics = _direction_aligned_market_move(result, market_context_snapshot)
    if metrics is None:
        return "market_context_unavailable"
    aligned_return, aligned_price_move, atr_20d = metrics
    if aligned_return > return_threshold:
        return "already_priced"
    if atr_20d is not None and atr_20d > 0 and aligned_price_move > atr_multiple * atr_20d:
        return "already_priced"
    return None


def _already_priced_thresholds(
    strategy: ThesisStrategy,
    *,
    event_driven_atr_multiple: float,
    event_driven_return_threshold: float,
    sentiment_momentum_atr_multiple: float,
    sentiment_momentum_return_threshold: float,
) -> tuple[float, float] | None:
    if strategy is ThesisStrategy.EVENT_DRIVEN:
        return event_driven_atr_multiple, event_driven_return_threshold
    if strategy is ThesisStrategy.SENTIMENT_MOMENTUM:
        return sentiment_momentum_atr_multiple, sentiment_momentum_return_threshold
    return None


def _direction_aligned_market_move(
    result: LlmAnalysisResult,
    market_context_snapshot: dict[str, Any] | None,
) -> tuple[float, float, float | None] | None:
    if market_context_snapshot is None:
        return None
    status = str(market_context_snapshot.get("source_status") or "").lower()
    if status not in {"fresh", "delayed"}:
        return None
    sign = 1.0 if result.direction is TradeDirection.BUY else -1.0 if result.direction is TradeDirection.SELL else 0.0
    if sign == 0:
        return None

    raw_return = _float_or_none(market_context_snapshot.get("return_1d"))
    current_price = _float_or_none(market_context_snapshot.get("current_price"))
    previous_close = _float_or_none(market_context_snapshot.get("previous_close"))
    if raw_return is None and current_price is not None and previous_close not in {None, 0.0}:
        raw_return = (current_price - previous_close) / previous_close
    if raw_return is None or current_price is None or previous_close is None:
        return None
    atr_20d = _float_or_none(market_context_snapshot.get("atr_20d"))
    aligned_return = sign * raw_return
    aligned_price_move = sign * (current_price - previous_close)
    return aligned_return, aligned_price_move, atr_20d


def _tradeability_rejection(
    *,
    market_context_snapshot: dict[str, Any] | None,
    tradeability_max_entry_price: float,
    risk_max_loss_usd: float,
    atr_stop_mult: float,
) -> str | None:
    if market_context_snapshot is None:
        return "market_context_unavailable"
    status = str(market_context_snapshot.get("source_status") or "").lower()
    if status not in {"fresh", "delayed"}:
        return "market_context_unavailable"
    current_price = _float_or_none(market_context_snapshot.get("current_price"))
    atr_20d = _float_or_none(market_context_snapshot.get("atr_20d"))
    if current_price is None or current_price <= 0 or atr_20d is None or atr_20d <= 0:
        return "market_context_unavailable"
    if current_price > tradeability_max_entry_price:
        return "untradeable_risk_box"
    if atr_stop_mult * atr_20d > risk_max_loss_usd:
        return "untradeable_risk_box"
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
                "event_occurred_at": (
                    _to_utc(analysis.event_occurred_at).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if analysis.event_occurred_at
                    else None
                ),
                "effective_evidence_at": _effective_evidence_at(analysis).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return evidence


def _synthesis_dossier(
    *,
    result: LlmAnalysisResult,
    selected: list[PersistedAnalysis],
    evidence: list[dict[str, Any]],
    market_context_snapshot: dict[str, Any] | None,
    risk_box: dict[str, Any],
    default_time_horizon: str,
) -> dict[str, Any]:
    return {
        "candidate": {
            "ticker": result.ticker,
            "exchange_code": result.exchange_code,
            "strategy": result.candidate_strategy.value,
            "direction": result.direction.value,
            "time_horizon": default_time_horizon,
            "per_article_confidence_mean": (
                sum(item.confidence for item in selected) / len(selected)
            ),
        },
        "deterministic_gate_results": {
            "evidence_count_satisfied": True,
            "freshness_satisfied": True,
            "already_priced_satisfied": True,
        },
        "evidence": evidence,
        "analyses": [
            {
                "analysis_id": item.id,
                "article_id": item.article_id,
                "confidence": item.confidence,
                "reasoning": item.reasoning,
                "article": _article_snapshot(item.article),
            }
            for item in selected
        ],
        "market_context": market_context_snapshot,
        "mechanical_risk_box": risk_box,
    }


def _synthesized_evidence(result: LlmSynthesisResult) -> list[dict[str, Any]]:
    evidence = [{"summary": result.thesis_summary, "source": "card_synthesis"}]
    evidence.extend(
        {"text": bullet, "source": "card_synthesis"}
        for bullet in result.evidence_bullets
    )
    if result.risk_rationale:
        evidence.append({"risk_rationale": result.risk_rationale, "source": "card_synthesis"})
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


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "article_ids": list(row["article_ids"] or []),
        "analysis_ids": [int(item) for item in row["analysis_ids"] or []],
        "window_started_at": _to_utc(row["window_started_at"]),
        "last_evidence_at": _to_utc(row["last_evidence_at"]),
        "required_evidence_count": int(row["required_evidence_count"] or 3),
        "story_narrative": row.get("story_narrative"),
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
        subject_relation=(
            SubjectRelation(str(row["subject_relation"]))
            if row.get("subject_relation")
            else None
        ),
        event_occurred_at=(
            _to_utc(row["event_occurred_at"]) if row.get("event_occurred_at") else None
        ),
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


def _effective_evidence_at(analysis: PersistedAnalysis) -> datetime:
    published_at = _to_utc(analysis.article.published_at)
    if analysis.event_occurred_at is None:
        return published_at
    event_occurred_at = _to_utc(analysis.event_occurred_at)
    return min(published_at, event_occurred_at)


def _story_narrative(*, article: NewsArticle, result: LlmAnalysisResult) -> str:
    parts = [f"Headline: {article.headline.strip()}"]
    if result.event_type:
        parts.append(f"Event type: {result.event_type.strip()}")
    bullets = [bullet.strip() for bullet in result.evidence_bullet_candidates if bullet.strip()]
    if bullets:
        parts.append("Evidence bullets:")
        parts.extend(f"- {bullet}" for bullet in bullets)
    elif article.summary:
        parts.append(f"Summary: {article.summary.strip()}")
    return "\n".join(parts)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
