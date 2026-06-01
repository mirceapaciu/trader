from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core_components.event_ingestion_engine.engine import EventIngestionEngine
from src.core_components.event_ingestion_engine.errors import (
    NonTransientPublishError,
    TransientPublishError,
)
from src.core_components.event_ingestion_engine.interfaces import (
    EventPublisher,
    InboundSourceAdapter,
    StorageAdapter,
)
from src.core_components.event_ingestion_engine.models import (
    CanonicalEvent,
    Checkpoint,
    FetchedBatch,
    FilterOutcome,
    FilterRun,
    FilterRunMode,
    FilterResult,
    PublicationObligation,
    PublicationStatus,
    SoftDedupePolicy,
    SourceEvent,
)


class FakeSourceAdapter(InboundSourceAdapter):
    def __init__(self, batch: FetchedBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[str, Any]] = []

    def fetch(self, source_key: str, cursor: Any | None) -> FetchedBatch:
        self.calls.append((source_key, cursor))
        return self.batch


class InMemoryStorage(StorageAdapter):
    def __init__(self) -> None:
        self.checkpoints: dict[str, Checkpoint] = {
            "finnhub": Checkpoint(
                source_key="finnhub",
                cursor_value={"cursor": "old"},
                cursor_updated_at=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
                version=4,
            )
        }
        self.events: dict[str, CanonicalEvent] = {}
        self.candidate_events: dict[str, CanonicalEvent] = {}
        self.filter_results: list[FilterResult] = []
        self.filter_run: FilterRun | None = None
        self.obligations: dict[str, PublicationObligation] = {}
        self.batch_to_ids: dict[str, list[str]] = {}

    def get_checkpoint(self, source_key: str) -> Checkpoint | None:
        return self.checkpoints.get(source_key)

    def list_soft_dedupe_candidates(
        self,
        *,
        source: str,
        occurred_at: datetime,
        lookback_window: timedelta,
    ) -> list[CanonicalEvent]:
        lookback_start = occurred_at - lookback_window
        return [
            event
            for event in self.events.values()
            if event.source == source and lookback_start <= event.occurred_at <= occurred_at
        ]

    def persist_batch(
        self,
        *,
        source_key: str,
        batch_id: str,
        filter_run,
        candidate_events,
        accepted_events,
        filter_results,
        obligations,
    ) -> None:
        self.filter_run = filter_run
        for event in candidate_events:
            self.candidate_events.setdefault(event.id, event)
        for event in accepted_events:
            self.events.setdefault(event.id, event)
        self.filter_results.extend(filter_results)
        ids: list[str] = []
        for obligation in obligations:
            self.obligations[obligation.obligation_id] = obligation
            ids.append(obligation.obligation_id)
        self.batch_to_ids[batch_id] = ids

    def load_batch_obligations(self, *, batch_id: str):
        return [self.obligations[oid] for oid in self.batch_to_ids.get(batch_id, [])]

    def mark_obligation_status(
        self,
        *,
        obligation_id: str,
        status: PublicationStatus,
        last_error_code: str | None,
    ) -> None:
        self.obligations[obligation_id] = replace(
            self.obligations[obligation_id], status=status, last_error_code=last_error_code
        )

    def has_non_terminal_obligations(self, *, batch_id: str) -> bool:
        for oid in self.batch_to_ids.get(batch_id, []):
            if self.obligations[oid].status in (PublicationStatus.PENDING, PublicationStatus.PUBLISHING):
                return True
        return False

    def advance_checkpoint(
        self,
        *,
        source_key: str,
        expected_version: int,
        new_cursor: Any,
        cursor_updated_at: datetime,
    ) -> bool:
        current = self.checkpoints.get(source_key)
        if current and current.version != expected_version:
            return False
        self.checkpoints[source_key] = Checkpoint(
            source_key=source_key,
            cursor_value=new_cursor,
            cursor_updated_at=cursor_updated_at,
            version=expected_version + 1,
        )
        return True


class FakePublisher(EventPublisher):
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.published: list[dict[str, Any]] = []

    def publish(self, envelope: dict[str, Any]) -> None:
        if self.mode == "transient_error":
            raise TransientPublishError("temporary_broker_failure")
        if self.mode == "non_transient_error":
            raise NonTransientPublishError("invalid_envelope")
        self.published.append(envelope)


def _source_event(title: str) -> SourceEvent:
    return SourceEvent(
        source="finnhub",
        source_event_id="provider-1",
        canonical_locator="https://example.com/news/1",
        title=title,
        summary="Revenue beat expectations",
        occurred_at=datetime(2026, 5, 25, 10, 30, 0, tzinfo=timezone.utc),
        attributes={"pre_filter_outcome": "accepted", "pre_filter_reason_code": None},
    )


def test_process_source_advances_checkpoint_after_successful_publish() -> None:
    batch = FetchedBatch(
        events=[_source_event("Company X beats estimates")],
        next_cursor={"cursor": "new"},
        cursor_updated_at=datetime(2026, 5, 25, 10, 31, 0, tzinfo=timezone.utc),
    )
    source = FakeSourceAdapter(batch)
    storage = InMemoryStorage()
    publisher = FakePublisher(mode="ok")

    engine = EventIngestionEngine(
        source_adapter=source,
        storage_adapter=storage,
        publisher=publisher,
        producer="news_fetcher",
        event_type="news.article.created",
        dedupe_policy=SoftDedupePolicy(),
        filter_run=FilterRun(
            filter_run_id="prod_test",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint="fingerprint",
        ),
    )

    result = engine.process_source("finnhub")

    assert result.fetched == 1
    assert result.accepted == 1
    assert result.rejected == 0
    assert result.checkpoint_advanced is True
    assert storage.filter_run is not None
    assert storage.filter_run.filter_run_id == "prod_test"
    assert storage.checkpoints["finnhub"].cursor_value == {"cursor": "new"}
    assert len(storage.candidate_events) == 1
    assert storage.filter_results == [
        FilterResult(article_id=next(iter(storage.candidate_events)), outcome=FilterOutcome.ACCEPTED)
    ]
    assert len(publisher.published) == 1


def test_process_source_blocks_checkpoint_on_transient_publish_failure() -> None:
    batch = FetchedBatch(
        events=[_source_event("Company X beats estimates")],
        next_cursor={"cursor": "new"},
        cursor_updated_at=datetime(2026, 5, 25, 10, 31, 0, tzinfo=timezone.utc),
    )
    source = FakeSourceAdapter(batch)
    storage = InMemoryStorage()
    publisher = FakePublisher(mode="transient_error")

    engine = EventIngestionEngine(
        source_adapter=source,
        storage_adapter=storage,
        publisher=publisher,
        producer="news_fetcher",
        event_type="news.article.created",
        filter_run=FilterRun(
            filter_run_id="prod_test",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint="fingerprint",
        ),
    )

    result = engine.process_source("finnhub")

    assert result.accepted == 1
    assert result.checkpoint_advanced is False


def test_process_source_can_advance_checkpoint_when_non_transient_dead_letters() -> None:
    batch = FetchedBatch(
        events=[_source_event("Company X beats estimates")],
        next_cursor={"cursor": "new"},
        cursor_updated_at=datetime(2026, 5, 25, 10, 31, 0, tzinfo=timezone.utc),
    )
    source = FakeSourceAdapter(batch)
    storage = InMemoryStorage()
    publisher = FakePublisher(mode="non_transient_error")

    engine = EventIngestionEngine(
        source_adapter=source,
        storage_adapter=storage,
        publisher=publisher,
        producer="news_fetcher",
        event_type="news.article.created",
        filter_run=FilterRun(
            filter_run_id="prod_test",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint="fingerprint",
        ),
        dead_letter_completes_obligation=True,
    )

    result = engine.process_source("finnhub")

    assert result.checkpoint_advanced is True


def test_process_source_records_pre_filter_rejections_without_publishing() -> None:
    batch = FetchedBatch(
        events=[
            SourceEvent(
                source="finnhub",
                source_event_id="provider-2",
                canonical_locator="https://example.com/news/2",
                title="Broad market commentary",
                summary="No watchlist relevance",
                occurred_at=datetime(2026, 5, 25, 10, 35, 0, tzinfo=timezone.utc),
                attributes={
                    "pre_filter_outcome": "rejected",
                    "pre_filter_reason_code": "rejected_not_relevant",
                },
            )
        ],
        next_cursor={"cursor": "new"},
        cursor_updated_at=datetime(2026, 5, 25, 10, 36, 0, tzinfo=timezone.utc),
    )
    source = FakeSourceAdapter(batch)
    storage = InMemoryStorage()
    publisher = FakePublisher(mode="ok")

    engine = EventIngestionEngine(
        source_adapter=source,
        storage_adapter=storage,
        publisher=publisher,
        producer="news_fetcher",
        event_type="news.article.created",
        filter_run=FilterRun(
            filter_run_id="prod_test",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint="fingerprint",
        ),
    )

    result = engine.process_source("finnhub")

    assert result.fetched == 1
    assert result.accepted == 0
    assert result.rejected == 1
    assert publisher.published == []
    assert storage.filter_results == [
        FilterResult(
            article_id=next(iter(storage.candidate_events)),
            outcome=FilterOutcome.REJECTED,
            rejection_reason_code="rejected_not_relevant",
        )
    ]
