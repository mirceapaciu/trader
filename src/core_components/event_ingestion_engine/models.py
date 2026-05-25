"""Domain models for the event ingestion engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class SourceEvent:
    """Draft event shape emitted by source adapters before canonicalization."""

    source: str
    source_event_id: str | None
    canonical_locator: str
    title: str
    occurred_at: datetime
    summary: str | None = None
    content_text: str | None = None
    entities: list[str] | None = None
    attributes: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None


@dataclass(frozen=True)
class FetchedBatch:
    """A source-adapter fetch result plus the next cursor checkpoint value."""

    events: list[SourceEvent]
    next_cursor: Any
    cursor_updated_at: datetime


@dataclass(frozen=True)
class Checkpoint:
    """Replay-safe progress marker for one source adapter."""

    source_key: str
    cursor_value: Any
    cursor_updated_at: datetime
    version: int


@dataclass(frozen=True)
class CanonicalEvent:
    """Canonical event schema v1."""

    id: str
    source: str
    source_event_id: str
    canonical_locator: str
    title: str
    occurred_at: datetime
    ingested_at: datetime
    payload_version: str = "1.0"
    summary: str | None = None
    content_text: str | None = None
    entities: list[str] | None = None
    attributes: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None


@dataclass(frozen=True)
class SoftDedupePolicy:
    """Policy parameters for the required soft dedupe implementation."""

    enabled: bool = True
    algorithm: str = "weighted_text_locator_v1"
    algorithm_version: str = "1.0"
    threshold: float = 0.85
    lookback_window: timedelta = timedelta(hours=72)
    max_time_delta_hours: int = 72


@dataclass(frozen=True)
class DedupeDecision:
    """Dedupe/filter decision for one canonical event."""

    accepted: bool
    reason_code: str
    matched_event_id: str | None = None
    similarity_score: float | None = None
    algorithm_version: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)


class PublicationStatus(StrEnum):
    """Publication obligation lifecycle states."""

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True)
class PublicationObligation:
    """Durable publication obligation (transactional outbox record)."""

    obligation_id: str
    source_key: str
    batch_id: str
    canonical_event_id: str
    event_type: str
    dedupe_key: str
    envelope_json: dict[str, Any]
    status: PublicationStatus = PublicationStatus.PENDING
    attempt_count: int = 0
    last_error_code: str | None = None
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None


@dataclass(frozen=True)
class ProcessResult:
    """Summary of one end-to-end source processing pass."""

    fetched: int
    accepted: int
    rejected: int
    checkpoint_advanced: bool
