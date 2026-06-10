from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HealthState = Literal["healthy", "unhealthy"]
DependencyKind = Literal["postgres", "redis"]


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
    buckets: list[ThroughputBucket]
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
    url: str
    source: str
    published_at: datetime
    production_filter_outcome: str | None = None
    simulation_filter_outcome: str | None = None
    rejection_reason_code: str | None = None
    probable_cause: str | None = None
    improvement_suggestion: str | None = None
    rationale: str | None = None
    classification_confidence: float | None = None
    suggestion_json: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime


class FilterQualityIncorrectlyRejectedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    items: list[FilterQualityIncorrectlyRejectedItem]
    generated_at: datetime


class FilterQualityStartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]
