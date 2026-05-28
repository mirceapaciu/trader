from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
