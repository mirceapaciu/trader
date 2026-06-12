from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core_components.event_ingestion_engine.errors import TransientPublishError
from src.core_components.event_ingestion_engine.interfaces import EventPublisher, StorageAdapter
from src.core_components.event_ingestion_engine.models import (
    CanonicalEvent,
    Checkpoint,
    FilterRun,
    PublicationObligation,
    PublicationStatus,
)
from src.product_components.news_fetcher.filter_config import NewsFilterConfig
from src.product_components.news_fetcher.providers import (
    NewsProvider,
    ProviderRateLimitError,
    ProviderArticle,
    ProviderBatch,
)
from src.product_components.news_fetcher.rss_feeds import RssFeedSpec
from src.product_components.news_fetcher.service import NewsFetcherService
from src.product_components.news_fetcher.settings import NewsFetcherSettings


class FakeProvider(NewsProvider):
    def __init__(self, batch: ProviderBatch) -> None:
        self.batch = batch

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        return self.batch


class RateLimitedProvider(NewsProvider):
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        self.calls += 1
        raise ProviderRateLimitError("provider_rate_limited")


class InMemoryStorage(StorageAdapter):
    def __init__(self) -> None:
        self.checkpoints: dict[str, Checkpoint] = {}
        self.events: dict[str, CanonicalEvent] = {}
        self.obligations: dict[str, PublicationObligation] = {}
        self.batch_to_ids: dict[str, list[str]] = {}
        self.watchlist = {"AAPL"}
        self.rss_feed_specs: list[RssFeedSpec] = []
        self.cycle_statuses: list[dict[str, Any]] = []
        self.filter_config: NewsFilterConfig | None = None
        self.filter_runs: list[FilterRun] = []
        self.claimed_retry_batches: list[dict[str, Any]] = []

    def load_active_watchlist_tickers(self) -> set[str]:
        return self.watchlist

    def load_rss_feed_specs(self) -> list[RssFeedSpec]:
        return self.rss_feed_specs

    def seed_production_filter_config_if_missing(
        self,
        *,
        include_keywords: tuple[str, ...],
        exclude_keywords: tuple[str, ...],
        watchlist_tickers: set[str],
        dedupe_algorithm: str,
        dedupe_similarity_threshold: float,
        dedupe_lookback_hours: int,
    ) -> NewsFilterConfig:
        if self.filter_config is None:
            self.filter_config = NewsFilterConfig(
                filter_config_id="test_cfg",
                config_name="Production filter",
                config_role="production",
                status="active",
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                watchlist_tickers=tuple(sorted(watchlist_tickers)),
                dedupe_algorithm=dedupe_algorithm,
                dedupe_similarity_threshold=dedupe_similarity_threshold,
                dedupe_lookback_hours=dedupe_lookback_hours,
            )
        return self.filter_config

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
        self.filter_runs.append(filter_run)
        for event in accepted_events:
            self.events.setdefault(event.id, event)
        ids: list[str] = []
        for obligation in obligations:
            self.obligations[obligation.obligation_id] = obligation
            ids.append(obligation.obligation_id)
        self.batch_to_ids[batch_id] = ids

    def load_batch_obligations(self, *, batch_id: str):
        return [self.obligations[oid] for oid in self.batch_to_ids.get(batch_id, [])]

    def claim_retryable_obligations(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[PublicationObligation]:
        now = datetime.now(timezone.utc)
        claimed: list[PublicationObligation] = []
        for obligation in sorted(self.obligations.values(), key=lambda item: (item.batch_id, item.obligation_id)):
            is_retryable = obligation.status == PublicationStatus.PENDING or (
                obligation.status == PublicationStatus.PUBLISHING
                and obligation.claim_expires_at is not None
                and obligation.claim_expires_at < now
            )
            if not is_retryable:
                continue
            updated = replace(
                obligation,
                status=PublicationStatus.PUBLISHING,
                attempt_count=obligation.attempt_count + 1,
                claimed_by=worker_id,
                claim_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self.obligations[obligation.obligation_id] = updated
            claimed.append(updated)
            if len(claimed) >= limit:
                break
        self.claimed_retry_batches.append(
            {"worker_id": worker_id, "limit": limit, "lease_seconds": lease_seconds, "count": len(claimed)}
        )
        return claimed

    def mark_obligation_status(
        self,
        *,
        obligation_id: str,
        status: PublicationStatus,
        last_error_code: str | None,
    ) -> None:
        self.obligations[obligation_id] = replace(
            self.obligations[obligation_id],
            status=status,
            last_error_code=last_error_code,
            claimed_by=None if status in (PublicationStatus.PUBLISHED, PublicationStatus.DEAD_LETTERED, PublicationStatus.PENDING) else self.obligations[obligation_id].claimed_by,
            claim_expires_at=None if status in (PublicationStatus.PUBLISHED, PublicationStatus.DEAD_LETTERED, PublicationStatus.PENDING) else self.obligations[obligation_id].claim_expires_at,
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
        rss_enabled=True,
        rss_rate_limit_backoff_seconds=900,
        instruments_config="",
        rss_sources_config="",
        legacy_rss_feed_urls=(),
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        publish_retry_drain_batch_size=500,
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
    assert storage.cycle_statuses[0]["last_non_zero_fetch_at"] is None


def test_service_records_last_non_zero_fetch_timestamp_for_non_empty_batches() -> None:
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

    service.run_once()

    assert len(storage.cycle_statuses) == 1
    assert storage.cycle_statuses[0]["last_non_zero_fetch_at"] is not None
    assert storage.cycle_statuses[0]["last_non_zero_fetch_at"].tzinfo == timezone.utc


def test_service_processes_dynamic_rss_feed_specs(monkeypatch) -> None:
    parsed = type(
        "ParsedFeed",
        (),
        {
            "entries": [
                type(
                    "Entry",
                    (),
                    {
                        "id": "rss-1",
                        "title": "Apple raises guidance",
                        "link": "https://example.com/rss/aapl",
                        "summary": "Quarterly outlook improved",
                        "published": "Wed, 28 May 2026 00:00:00 GMT",
                    },
                )()
            ]
        },
    )()

    def _fake_parse(feed_url, *, timeout_seconds):
        return parsed

    monkeypatch.setattr(
        "src.product_components.news_fetcher.providers._parse_rss_feed",
        _fake_parse,
    )

    storage = InMemoryStorage()
    storage.rss_feed_specs = [
        RssFeedSpec(
            source_key="rss:yahoo_finance:AAPL:NASDAQ",
            provider_name="yahoo_finance",
            url="https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US",
            app_tickers=("AAPL",),
            min_request_interval_seconds=0,
        )
    ]
    publisher = FakePublisher()
    service = NewsFetcherService(
        settings=_settings(),
        providers={},
        storage=storage,
        publisher=publisher,
    )

    results = service.run_once()

    assert "rss:yahoo_finance:AAPL:NASDAQ" in results
    assert results["rss:yahoo_finance:AAPL:NASDAQ"].accepted == 1
    assert len(publisher.published) == 1


def test_service_uses_db_backed_filter_config_when_available() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[
                ProviderArticle(
                    source="finnhub",
                    headline="Apple reports strategic partnership",
                    url="https://example.com/aapl",
                    published_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    fetched_at=datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
                    tickers=[],
                )
            ],
            next_cursor={"cursor": "new"},
            cursor_updated_at=datetime(2026, 5, 27, 9, 1, tzinfo=timezone.utc),
        )
    )
    storage = InMemoryStorage()
    storage.filter_config = NewsFilterConfig(
        filter_config_id="db_cfg",
        config_name="Production filter",
        config_role="production",
        status="active",
        include_keywords=("strategic partnership",),
        exclude_keywords=(),
        watchlist_tickers=(),
        dedupe_algorithm="rapidfuzz_ratio",
        dedupe_similarity_threshold=0.9,
        dedupe_lookback_hours=24,
    )
    service = NewsFetcherService(
        settings=_settings(),
        providers={"finnhub": provider},
        storage=storage,
        publisher=FakePublisher(),
    )

    result = service.run_once()["finnhub"]

    assert result.accepted == 1
    assert storage.filter_runs[0].filter_config_snapshot_json["include_keywords"] == ["strategic partnership"]


def test_service_backs_off_rate_limited_sources() -> None:
    provider = RateLimitedProvider()
    storage = InMemoryStorage()
    publisher = FakePublisher()
    service = NewsFetcherService(
        settings=_settings(),
        providers={"rss:yahoo_finance:AXA:PARIS": provider},
        storage=storage,
        publisher=publisher,
    )

    first = service.run_once()
    second = service.run_once()

    assert first == {}
    assert second == {}
    assert provider.calls == 1
    assert storage.cycle_statuses[0]["error_code"] == "provider_rate_limited"
    assert storage.cycle_statuses[1]["error_code"] == "provider_rate_limit_backoff"


def test_service_respects_generated_rss_min_request_interval(monkeypatch) -> None:
    parsed = type(
        "ParsedFeed",
        (),
        {
            "entries": [
                type(
                    "Entry",
                    (),
                    {
                        "id": "rss-1",
                        "title": "Apple raises guidance",
                        "link": "https://example.com/rss/aapl",
                        "summary": "Quarterly outlook improved",
                        "published": "Wed, 28 May 2026 00:00:00 GMT",
                    },
                )()
            ]
        },
    )()
    calls = []

    def _fake_parse(feed_url, *, timeout_seconds):
        calls.append(feed_url)
        return parsed

    monkeypatch.setattr(
        "src.product_components.news_fetcher.providers._parse_rss_feed",
        _fake_parse,
    )

    storage = InMemoryStorage()
    storage.rss_feed_specs = [
        RssFeedSpec(
            source_key="rss:yahoo_finance:batch:abc",
            provider_name="yahoo_finance",
            url="https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US",
            app_tickers=("AAPL",),
            min_request_interval_seconds=900,
        )
    ]
    service = NewsFetcherService(
        settings=_settings(),
        providers={},
        storage=storage,
        publisher=FakePublisher(),
    )

    service.run_once()
    service.run_once()

    assert len(calls) == 1
    assert storage.cycle_statuses[1]["error_code"] == "source_interval_wait"


def test_service_drains_existing_pending_obligations_before_fetching() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[],
            next_cursor={"cursor": "same"},
            cursor_updated_at=datetime(2026, 5, 27, 9, 1, tzinfo=timezone.utc),
        )
    )
    storage = InMemoryStorage()
    obligation = PublicationObligation(
        obligation_id="obl_pending",
        source_key="finnhub",
        batch_id="batch_old",
        canonical_event_id="article_1",
        event_type="news.article.created",
        dedupe_key="article_1",
        envelope_json={"event_id": "evt_1", "payload": {"id": "article_1"}},
        status=PublicationStatus.PENDING,
    )
    storage.obligations[obligation.obligation_id] = obligation
    publisher = FakePublisher()
    service = NewsFetcherService(
        settings=_settings(),
        providers={"finnhub": provider},
        storage=storage,
        publisher=publisher,
    )

    service.run_once()

    assert len(publisher.published) == 1
    assert storage.obligations["obl_pending"].status == PublicationStatus.PUBLISHED
    assert storage.obligations["obl_pending"].attempt_count == 1
    assert storage.claimed_retry_batches[0]["count"] == 1


def test_service_requeues_retryable_obligation_after_transient_publish_failure() -> None:
    storage = InMemoryStorage()
    stale_obligation = PublicationObligation(
        obligation_id="obl_stale",
        source_key="finnhub",
        batch_id="batch_old",
        canonical_event_id="article_2",
        event_type="news.article.created",
        dedupe_key="article_2",
        envelope_json={"event_id": "evt_2", "payload": {"id": "article_2"}},
        status=PublicationStatus.PUBLISHING,
        attempt_count=2,
        claimed_by="worker_old",
        claim_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    storage.obligations[stale_obligation.obligation_id] = stale_obligation
    service = NewsFetcherService(
        settings=_settings(),
        providers={},
        storage=storage,
        publisher=FakePublisher(mode="transient_error"),
    )

    service.run_once()

    assert storage.obligations["obl_stale"].status == PublicationStatus.PENDING
    assert storage.obligations["obl_stale"].attempt_count == 3
    assert storage.obligations["obl_stale"].last_error_code == "temporary_broker_failure"
    assert storage.obligations["obl_stale"].claimed_by is None


def test_service_uses_configured_retry_drain_batch_size() -> None:
    storage = InMemoryStorage()
    for index in range(3):
        obligation = PublicationObligation(
            obligation_id=f"obl_{index}",
            source_key="finnhub",
            batch_id="batch_old",
            canonical_event_id=f"article_{index}",
            event_type="news.article.created",
            dedupe_key=f"article_{index}",
            envelope_json={"event_id": f"evt_{index}", "payload": {"id": f"article_{index}"}},
            status=PublicationStatus.PENDING,
        )
        storage.obligations[obligation.obligation_id] = obligation
    settings = replace(_settings(), publish_retry_drain_batch_size=2)
    service = NewsFetcherService(
        settings=settings,
        providers={},
        storage=storage,
        publisher=FakePublisher(),
    )

    service.run_once()

    assert storage.claimed_retry_batches[0]["limit"] == 2
    assert storage.claimed_retry_batches[0]["count"] == 2
    published_count = sum(1 for obligation in storage.obligations.values() if obligation.status == PublicationStatus.PUBLISHED)
    pending_count = sum(1 for obligation in storage.obligations.values() if obligation.status == PublicationStatus.PENDING)
    assert published_count == 2
    assert pending_count == 1
