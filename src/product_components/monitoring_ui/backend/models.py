from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HealthState = Literal["healthy", "unhealthy"]
DependencyKind = Literal["postgres", "redis"]
ThroughputGranularity = Literal["raw", "hour", "day"]


class DependencyHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: DependencyKind
    state: HealthState
    message: str | None = None
    checked_at: datetime


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    readiness: HealthState
    liveness: HealthState
    stale_data: bool
    last_successful_refresh_at: datetime
    dependencies: list[DependencyHealth]
    active_incident_count: int


class ProviderStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_key: str
    last_cycle_start_at: datetime | None = None
    last_cycle_end_at: datetime | None = None
    last_cycle_duration_seconds: float | None = None
    last_fetch_attempt_at: datetime | None = None
    last_non_zero_fetch_at: datetime | None = None
    fetch_count: int = 0
    fetch_error_count: int = 0
    dedupe_drop_count: int = 0
    persist_success_count: int = 0
    publish_success_count: int = 0
    last_error_code: str | None = None
    last_error_at: datetime | None = None


class ProvidersResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    providers: list[ProviderStatus]
    generated_at: datetime


class ThroughputBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_start: datetime
    source_key: str
    fetch_count: int
    publish_success_count: int
    publish_error_count: int


class ThroughputResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: str
    granularity: ThroughputGranularity
    window_start_at: datetime
    window_end_at: datetime
    buckets: list[ThroughputBucket]
    generated_at: datetime


class ThesisBuilderPendingWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_id: int
    ticker: str
    exchange_code: str
    strategy: str
    direction: str | None = None
    window_started_at: datetime
    last_evidence_at: datetime
    pending_age_seconds: float
    expires_in_seconds: float


class ThesisBuilderMetricsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool = True
    message: str | None = None
    window: str
    window_start_at: datetime
    window_end_at: datetime
    articles_processed_count: int
    market_moving_articles_count: int
    articles_included_in_cards_count: int
    stale_articles_count: int
    created_thesis_cards_count: int
    pending_thesis_cards_count: int
    oldest_pending_age_seconds: float | None = None
    average_pending_age_seconds: float | None = None
    minimum_pending_expires_in_seconds: float | None = None
    average_pending_expires_in_seconds: float | None = None
    missed_stale_thesis_cards_count: int
    stale_evidence_exceeded_avg_seconds: float | None = None
    stale_evidence_exceeded_p95_seconds: float | None = None
    stale_evidence_exceeded_max_seconds: float | None = None
    pending_windows: list[ThesisBuilderPendingWindow] = Field(default_factory=list)
    generated_at: datetime


class BacklogResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending_count: int
    retrying_count: int
    dead_letter_count: int
    max_attempt_age_seconds: float | None = None
    generated_at: datetime


class DeadLetterItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    obligation_id: str
    source_key: str
    canonical_event_id: str
    reason: str | None = None
    first_failure_at: datetime | None = None
    updated_at: datetime


class DeadLetterResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[DeadLetterItem]
    limit: int
    offset: int
    generated_at: datetime


FilterQualityRunStatus = Literal["running", "completed", "failed"]


class NewsFilterConfigPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    filter_config_id: str | None = None
    config_name: str = "Test filter"
    config_role: str = "test"
    status: str = "active"
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    watchlist_tickers: list[str] = Field(default_factory=list)
    dedupe_algorithm: str = "rapidfuzz_ratio"
    dedupe_similarity_threshold: float = 0.9
    dedupe_lookback_hours: int = 24
    created_from_run_id: str | None = None


class FilterQualityRunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: FilterQualityRunStatus
    news_window_start_at: datetime
    news_window_end_at: datetime
    created_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    rejection_precision_proxy: float | None = None
    incorrectly_accepted_rate_estimate: float | None = None
    dataset_input_count: int
    dataset_rejected_count: int
    dataset_accepted_count: int
    rejected_items_evaluated: int
    accepted_items_sampled: int
    correctly_rejected_count: int
    incorrectly_rejected_count: int
    correctly_accepted_count: int
    incorrectly_accepted_count: int
    item_failed_count: int = 0
    item_error_codes: dict[str, int] = Field(default_factory=dict)
    total_filter_quality: float | None = None
    total_correct_count: int = 0
    assumed_correct_accepted_count: int = 0
    evaluation_subject: str = "unknown"
    evaluated_filter_config: NewsFilterConfigPayload | None = None
    summary_json: dict[str, Any]
    recommendation_summary_md: str


class FilterQualityStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    running_run: FilterQualityRunSummary | None
    last_run: FilterQualityRunSummary | None
    generated_at: datetime


class FilterQualityIncorrectlyRejectedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    assessment_id: str
    run_id: str
    article_id: str
    headline: str
    summary: str | None = None
    url: str
    source: str
    published_at: datetime
    production_matched_article_id: str | None = None
    production_matched_article_headline: str | None = None
    production_matched_article_url: str | None = None
    production_matched_article_source: str | None = None
    production_matched_article_published_at: datetime | None = None
    simulation_matched_article_id: str | None = None
    simulation_matched_article_headline: str | None = None
    simulation_matched_article_url: str | None = None
    simulation_matched_article_source: str | None = None
    simulation_matched_article_published_at: datetime | None = None
    production_filter_outcome: str | None = None
    simulation_filter_outcome: str | None = None
    rejection_reason_code: str | None = None
    production_rejection_reason_code: str | None = None
    simulation_rejection_reason_code: str | None = None
    probable_cause: str | None = None
    improvement_suggestion: str | None = None
    rationale: str | None = None
    classification_confidence: float | None = None
    suggestion_json: dict[str, Any] = Field(default_factory=dict)
    recommended_include_keywords: list[str] = Field(default_factory=list)
    evaluated_at: datetime


class FilterConfigSimulationStartResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]


class FilterQualityIncorrectlyRejectedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    items: list[FilterQualityIncorrectlyRejectedItem]
    generated_at: datetime


class FilterQualityIncorrectlyAcceptedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    assessment_id: str
    run_id: str
    article_id: str
    headline: str
    summary: str | None = None
    url: str
    source: str
    published_at: datetime
    production_filter_outcome: str | None = None
    simulation_filter_outcome: str | None = None
    probable_cause: str | None = None
    improvement_suggestion: str | None = None
    rationale: str | None = None
    classification_confidence: float | None = None
    suggestion_json: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime


class FilterQualityIncorrectlyAcceptedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    items: list[FilterQualityIncorrectlyAcceptedItem]
    generated_at: datetime


class FilterQualityStartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted_audit_enabled: bool = False


class FilterQualityStartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]
