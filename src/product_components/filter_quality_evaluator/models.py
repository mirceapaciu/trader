from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any

from src.core_components.event_ingestion_engine.models import CanonicalEvent, FilterResult


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationScope(StrEnum):
    REJECTED_POPULATION = "rejected_population"
    ACCEPTED_AUDIT = "accepted_audit"


class ItemStatus(StrEnum):
    EVALUATED = "evaluated"
    FAILED = "failed"


class ClassificationLabel(StrEnum):
    CORRECTLY_REJECTED = "correctly_rejected"
    INCORRECTLY_REJECTED = "incorrectly_rejected"
    CORRECTLY_ACCEPTED = "correctly_accepted"
    INCORRECTLY_ACCEPTED = "incorrectly_accepted"


class ProbableCause(StrEnum):
    KEYWORD_GAP = "keyword_gap"
    WATCHLIST_COVERAGE_GAP = "watchlist_coverage_gap"
    DEDUPE_THRESHOLD_ISSUE = "dedupe_threshold_issue"
    RULE_CONFLICT = "rule_conflict"
    LOW_VALUE_NOISE = "low_value_noise"


@dataclass(frozen=True)
class FilterQualityRunParams:
    run_id: str
    news_window_start_at: datetime
    news_window_end_at: datetime
    filter_config_fingerprint: str | None = None
    filter_config_snapshot_json: dict[str, Any] | None = None
    run_note: str | None = None
    accepted_audit_enabled: bool = False
    accepted_audit_sample_size: int | None = None


@dataclass(frozen=True)
class FilterSimulationConfig:
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    watchlist_tickers: set[str]
    dedupe_algorithm: str
    dedupe_similarity_threshold: float
    dedupe_lookback_hours: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "watchlist_tickers": sorted(self.watchlist_tickers),
            "dedupe_algorithm": self.dedupe_algorithm,
            "dedupe_similarity_threshold": self.dedupe_similarity_threshold,
            "dedupe_lookback_hours": self.dedupe_lookback_hours,
        }


@dataclass(frozen=True)
class InputArticle:
    id: str
    source: str
    headline: str
    summary: str | None
    url: str
    tickers: list[str]
    published_at: datetime
    fetched_at: datetime
    sentiment_source: float | None

    def canonical_event(self) -> CanonicalEvent:
        return CanonicalEvent(
            id=self.id,
            source=self.source,
            source_event_id=self.id,
            canonical_locator=self.url,
            title=self.headline,
            summary=self.summary,
            occurred_at=_to_utc(self.published_at),
            ingested_at=_to_utc(self.fetched_at),
            entities=self.tickers,
            attributes={
                "tickers": self.tickers,
                "sentiment_source": self.sentiment_source,
                "fetched_at": _to_utc(self.fetched_at).isoformat(),
            },
        )


@dataclass(frozen=True)
class ComparisonItem:
    article: InputArticle
    filter_run_id_production: str
    filter_run_id_simulation: str
    production_result: FilterResult | None
    simulation_result: FilterResult | None

    @property
    def is_complete(self) -> bool:
        return self.production_result is not None and self.simulation_result is not None

    @property
    def is_disagreement(self) -> bool:
        if not self.is_complete:
            return False
        return self.production_result.outcome.value != self.simulation_result.outcome.value


@dataclass(frozen=True)
class ClassificationResult:
    classification_label: ClassificationLabel
    classification_confidence: Decimal
    rationale: str
    probable_cause: ProbableCause
    improvement_suggestion: str
    suggestion_json: dict[str, Any] = field(default_factory=dict)
    llm_model: str | None = None
    estimated_tokens: int = 0


@dataclass(frozen=True)
class ItemAssessment:
    assessment_id: str
    run_id: str
    article_id: str
    evaluation_scope: EvaluationScope
    source: str
    published_at: datetime
    filter_run_id_production: str
    filter_run_id_simulation: str
    production_filter_outcome: str | None
    simulation_filter_outcome: str | None
    is_disagreement: bool
    rejection_reason_code: str | None
    item_status: ItemStatus
    item_error_code: str | None = None
    item_error_details_json: dict[str, Any] = field(default_factory=dict)
    classification_label: ClassificationLabel | None = None
    classification_confidence: Decimal | None = None
    rationale: str | None = None
    probable_cause: ProbableCause | None = None
    improvement_suggestion: str | None = None
    suggestion_json: dict[str, Any] = field(default_factory=dict)
    llm_model: str | None = None


@dataclass(frozen=True)
class RunSummary:
    dataset_input_count: int
    dataset_rejected_count: int
    dataset_accepted_count: int
    rejected_items_evaluated: int
    accepted_items_sampled: int
    correctly_rejected_count: int
    incorrectly_rejected_count: int
    correctly_accepted_count: int
    incorrectly_accepted_count: int
    rejection_precision_proxy: Decimal | None
    incorrectly_accepted_rate_estimate: Decimal | None
    summary_json: dict[str, Any]
    recommendation_summary_md: str


def quantize_rate(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.00001"),
        rounding=ROUND_HALF_UP,
    )


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
