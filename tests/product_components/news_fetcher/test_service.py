from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core_components.event_ingestion_engine.errors import TransientPublishError
from src.core_components.event_ingestion_engine.interfaces import EventPublisher, StorageAdapter
from src.core_components.event_ingestion_engine.models import (
    CanonicalEvent,
    Checkpoint,
    PublicationObligation,
    PublicationStatus,
)
from src.product_components.news_fetcher.providers import (
    NewsProvider,
    ProviderArticle,
    ProviderBatch,
)
from src.product_components.news_fetcher.service import NewsFetcherService
from src.product_components.news_fetcher.settings import NewsFetcherSettings


class FakeProvider(NewsProvider):
    def __init__(self, batch: ProviderBatch) -> None:
        self.batch = batch

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        return self.batch


class InMemoryStorage(StorageAdapter):
    def __init__(self) -> None:
        self.checkpoints: dict[str, Checkpoint] = {}
        self.events: dict[str, CanonicalEvent] = {}
        self.obligations: dict[str, PublicationObligation] = {}
        self.batch_to_ids: dict[str, list[str]] = {}
        self.watchlist = {"AAPL"}
        self.cycle_statuses: list[dict[str, Any]] = []

    def load_active_watchlist_tickers(self) -> set[str]:
        return self.watchlist

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
        accepted_events,
        obligations,
    ) -> None:
        for event in accepted_events:
            self.events.setdefault(event.id, event)
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
        return any(
            self.obligations[oid].status in (PublicationStatus.PENDING, PublicationStatus.PUBLISHING)
            for oid in self.batch_to_ids.get(batch_id, [])
        )

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

    def record_provider_cycle_status(self, **kwargs) -> None:
        self.cycle_statuses.append(kwargs)


class FakePublisher(EventPublisher):
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.published: list[dict[str, Any]] = []

    def publish(self, envelope: dict[str, Any]) -> None:
        if self.mode == "transient_error":
            raise TransientPublishError("temporary_broker_failure")
        self.published.append(envelope)


def _settings() -> NewsFetcherSettings:
    return NewsFetcherSettings(
        newsfetcher_db_schema="news_fetcher",
        shared_db_schema="shared",
        watchlist_table="t_watchlist_tickers",
        news_poll_interval=120,
        rss_poll_interval=300,
        marketaux_poll_interval=300,
        provider_timeout_seconds=5,
        provider_max_retries=3,
        provider_backoff_base_seconds=1,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        dedupe_lookback_hours=24,
        dedupe_similarity_threshold=0.9,
        dedupe_algorithm="rapidfuzz_ratio",
        include_keywords=("guidance",),
        exclude_keywords=(),
    )


def test_service_processes_provider_batches() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[
                ProviderArticle(
                    source="finnhub",
                    headline="Apple raises guidance",
                    url="https://example.com/aapl",
                    published_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    fetched_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    tickers=["AAPL"],
                )
            ],
            next_cursor={"cursor": "new"},
            cursor_updated_at=datetime(2026, 5, 27, 9, 1, tzinfo=timezone.utc),
        )
    )
    storage = InMemoryStorage()
    publisher = FakePublisher()
    service = NewsFetcherService(
        settings=_settings(),
        providers={"finnhub": provider},
        storage=storage,
        publisher=publisher,
    )

    results = service.run_once()

    assert "finnhub" in results
    assert results["finnhub"].accepted == 1
    assert results["finnhub"].checkpoint_advanced is True
    assert len(publisher.published) == 1


def test_service_continues_when_one_provider_fails() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[
                ProviderArticle(
                    source="finnhub",
                    headline="Apple raises guidance",
                    url="https://example.com/aapl",
                    published_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    fetched_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    tickers=["AAPL"],
                )
            ],
            next_cursor={"cursor": "new"},
            cursor_updated_at=datetime(2026, 5, 27, 9, 1, tzinfo=timezone.utc),
        )
    )
    storage = InMemoryStorage()
    publisher = FakePublisher(mode="transient_error")
    service = NewsFetcherService(
        settings=_settings(),
        providers={"finnhub": provider},
        storage=storage,
        publisher=publisher,
    )

    results = service.run_once()

    assert "finnhub" in results
    assert results["finnhub"].checkpoint_advanced is False


def test_service_records_cycle_status_for_empty_batches() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[],
            next_cursor={"cursor": "new"},
            cursor_updated_at=datetime(2026, 5, 27, 9, 1, tzinfo=timezone.utc),
        )
    )
    storage = InMemoryStorage()
    publisher = FakePublisher()
    service = NewsFetcherService(
        settings=_settings(),
        providers={"finnhub": provider},
        storage=storage,
        publisher=publisher,
    )

    results = service.run_once()

    assert results["finnhub"].fetched == 0
    assert len(storage.cycle_statuses) == 1
    assert storage.cycle_statuses[0]["source_key"] == "finnhub"
    assert storage.cycle_statuses[0]["status"] == "success"
    assert storage.cycle_statuses[0]["fetched_count"] == 0
