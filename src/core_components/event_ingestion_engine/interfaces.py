"""Protocols for adapters consumed by the event ingestion engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol, Sequence

from .models import (
    CanonicalEvent,
    Checkpoint,
    FetchedBatch,
    FilterRun,
    FilterResult,
    PublicationObligation,
    PublicationStatus,
)


class InboundSourceAdapter(Protocol):
    """Reads external events from one source and returns replay-safe cursor updates."""

    def fetch(self, source_key: str, cursor: Any | None) -> FetchedBatch:
        """Fetch source events from a given cursor position."""


class StorageAdapter(Protocol):
    """Durable persistence and outbox contract owned by the integrating component."""

    def get_checkpoint(self, source_key: str) -> Checkpoint | None:
        """Return checkpoint for one source key or None when not initialized."""

    def list_soft_dedupe_candidates(
        self,
        *,
        source: str,
        occurred_at: datetime,
        lookback_window: timedelta,
    ) -> Sequence[CanonicalEvent]:
        """Return persisted candidates for dedupe lookback."""

    def persist_batch(
        self,
        *,
        source_key: str,
        batch_id: str,
        filter_run: FilterRun,
        candidate_events: Sequence[CanonicalEvent],
        accepted_events: Sequence[CanonicalEvent],
        filter_results: Sequence[FilterResult],
        obligations: Sequence[PublicationObligation],
    ) -> None:
        """Atomically persist candidate events, filter results, accepted events, and obligations."""

    def load_batch_obligations(self, *, batch_id: str) -> Sequence[PublicationObligation]:
        """Load obligations created for one batch."""

    def mark_obligation_status(
        self,
        *,
        obligation_id: str,
        status: PublicationStatus,
        last_error_code: str | None,
    ) -> None:
        """Update obligation terminal/non-terminal state."""

    def has_non_terminal_obligations(self, *, batch_id: str) -> bool:
        """True when any batch obligation is still pending or publishing."""

    def advance_checkpoint(
        self,
        *,
        source_key: str,
        expected_version: int,
        new_cursor: Any,
        cursor_updated_at: datetime,
    ) -> bool:
        """Compare-and-set checkpoint advancement."""


class EventPublisher(Protocol):
    """Broker publication contract for outbox obligations."""

    def publish(self, envelope: dict[str, Any]) -> None:
        """Publish one fully-formed event envelope."""
