"""Event ingestion orchestration with persist->publish->checkpoint boundary."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from .canonicalization import canonicalize_source_event
from .deduplication import evaluate_dedupe
from .errors import NonTransientPublishError, TransientPublishError
from .interfaces import EventPublisher, InboundSourceAdapter, StorageAdapter
from .models import (
    ProcessResult,
    PublicationObligation,
    PublicationStatus,
    SoftDedupePolicy,
)


class EventIngestionEngine:
    """Reusable ingestion engine that enforces deterministic and idempotent flow."""

    def __init__(
        self,
        *,
        source_adapter: InboundSourceAdapter,
        storage_adapter: StorageAdapter,
        publisher: EventPublisher,
        producer: str,
        event_type: str,
        dedupe_policy: SoftDedupePolicy | None = None,
        dead_letter_completes_obligation: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source_adapter = source_adapter
        self._storage_adapter = storage_adapter
        self._publisher = publisher
        self._producer = producer
        self._event_type = event_type
        self._dedupe_policy = dedupe_policy or SoftDedupePolicy()
        self._dead_letter_completes_obligation = dead_letter_completes_obligation
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def process_source(self, source_key: str) -> ProcessResult:
        """Run one full source processing pass with checkpoint gating."""
        checkpoint = self._storage_adapter.get_checkpoint(source_key)
        batch = self._source_adapter.fetch(
            source_key, checkpoint.cursor_value if checkpoint else None
        )
        if not batch.events:
            return ProcessResult(fetched=0, accepted=0, rejected=0, checkpoint_advanced=False)

        batch_id = f"batch_{uuid.uuid4().hex}"
        accepted_events = []
        for source_event in batch.events:
            canonical = canonicalize_source_event(source_event, ingested_at=self._clock())
            candidates = list(
                self._storage_adapter.list_soft_dedupe_candidates(
                    source=canonical.source,
                    occurred_at=canonical.occurred_at,
                    lookback_window=self._dedupe_policy.lookback_window,
                )
            )
            decision = evaluate_dedupe(canonical, candidates, self._dedupe_policy)
            if decision.accepted:
                accepted_events.append(canonical)

        obligations = [
            self._build_obligation(source_key=source_key, batch_id=batch_id, canonical_event=event)
            for event in accepted_events
        ]

        self._storage_adapter.persist_batch(
            source_key=source_key,
            batch_id=batch_id,
            accepted_events=accepted_events,
            obligations=obligations,
        )

        self._publish_batch(batch_id=batch_id)

        checkpoint_advanced = False
        if not self._storage_adapter.has_non_terminal_obligations(batch_id=batch_id):
            expected_version = checkpoint.version if checkpoint else 0
            checkpoint_advanced = self._storage_adapter.advance_checkpoint(
                source_key=source_key,
                expected_version=expected_version,
                new_cursor=batch.next_cursor,
                cursor_updated_at=batch.cursor_updated_at,
            )

        return ProcessResult(
            fetched=len(batch.events),
            accepted=len(accepted_events),
            rejected=len(batch.events) - len(accepted_events),
            checkpoint_advanced=checkpoint_advanced,
        )

    def _build_obligation(self, *, source_key: str, batch_id: str, canonical_event) -> PublicationObligation:
        obligation_id = f"obl_{uuid.uuid4().hex}"
        envelope = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": self._event_type,
            "event_version": "1.0",
            "occurred_at": canonical_event.occurred_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published_at": self._clock().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "producer": self._producer,
            "dedupe_key": canonical_event.id,
            "payload": {
                "id": canonical_event.id,
                "source": canonical_event.source,
                "source_event_id": canonical_event.source_event_id,
                "canonical_locator": canonical_event.canonical_locator,
                "title": canonical_event.title,
                "summary": canonical_event.summary,
                "occurred_at": canonical_event.occurred_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ingested_at": canonical_event.ingested_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "payload_version": canonical_event.payload_version,
            },
        }
        return PublicationObligation(
            obligation_id=obligation_id,
            source_key=source_key,
            batch_id=batch_id,
            canonical_event_id=canonical_event.id,
            event_type=self._event_type,
            dedupe_key=canonical_event.id,
            envelope_json=envelope,
            status=PublicationStatus.PENDING,
        )

    def _publish_batch(self, *, batch_id: str) -> None:
        obligations = list(self._storage_adapter.load_batch_obligations(batch_id=batch_id))
        for obligation in obligations:
            if obligation.status in (PublicationStatus.PUBLISHED, PublicationStatus.DEAD_LETTERED):
                continue

            publishing = replace(obligation, status=PublicationStatus.PUBLISHING)
            self._storage_adapter.mark_obligation_status(
                obligation_id=publishing.obligation_id,
                status=PublicationStatus.PUBLISHING,
                last_error_code=None,
            )
            try:
                self._publisher.publish(publishing.envelope_json)
                self._storage_adapter.mark_obligation_status(
                    obligation_id=publishing.obligation_id,
                    status=PublicationStatus.PUBLISHED,
                    last_error_code=None,
                )
            except NonTransientPublishError as error:
                if self._dead_letter_completes_obligation:
                    self._storage_adapter.mark_obligation_status(
                        obligation_id=publishing.obligation_id,
                        status=PublicationStatus.DEAD_LETTERED,
                        last_error_code=str(error) or "non_transient_publish_error",
                    )
                else:
                    self._storage_adapter.mark_obligation_status(
                        obligation_id=publishing.obligation_id,
                        status=PublicationStatus.PENDING,
                        last_error_code=str(error) or "non_transient_publish_error",
                    )
            except TransientPublishError as error:
                self._storage_adapter.mark_obligation_status(
                    obligation_id=publishing.obligation_id,
                    status=PublicationStatus.PENDING,
                    last_error_code=str(error) or "transient_publish_error",
                )
