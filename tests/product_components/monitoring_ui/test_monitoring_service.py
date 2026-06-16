from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import redis

from src.product_components.monitoring_ui.backend.models import (
    AliasDiscoveryResponse,
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    FilterQualityIncorrectlyAcceptedItem,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedItem,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunRequest,
    FilterQualityRunSummary,
    FilterQualityStatusResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ProviderStatus,
    ThesisBuilderMetricsResponse,
    ThesisBuilderPendingWindow,
    ThroughputResponse,
    WatchlistItemPayload,
    WatchlistResponse,
)
from src.product_components.monitoring_ui.backend.filter_quality_runner import FilterQualityRunCoordinator
from src.product_components.monitoring_ui.backend.repository import (
    PostgresRedisMonitoringDataSource,
    _incorrectly_rejected_item,
    _incorrectly_accepted_item,
    _throughput_bucket_expression,
    _throughput_granularity_for_window,
)
from src.product_components.monitoring_ui.backend.service import (
    FilterQualityRunAlreadyActive,
    InvalidThroughputWindow,
    MonitoringService,
)
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings
from src.product_components.shared.adapters import SharedWatchlistEntryInput, SharedWatchlistRecord
from src.product_components.shared.instrument_lookup import (
    DuplicateActiveWatchlistEntry,
    InstrumentLookupSuggestion,
)


class FakeDataSource:
    def __init__(
        self,
        *,
        dependencies: list[DependencyHealth],
        providers: list[ProviderStatus],
    ) -> None:
        self.dependencies = dependencies
        self.providers = providers
        self.dead_letter_limit = 0
        self.dead_letter_offset = 0
        self.running_filter_quality_run: FilterQualityRunSummary | None = None
        self.last_filter_quality_run: FilterQualityRunSummary | None = None
        self.incorrectly_rejected_run_id: str | None = None
        self.incorrectly_accepted_run_id: str | None = None
        self.stale_timeout_seconds: int | None = None
        self.throughput_window: str | None = None
        self.throughput_start_at: datetime | None = None
        self.throughput_end_at: datetime | None = None
        self.thesis_builder_window: str | None = None
        self.thesis_builder_evidence_collection_max_minutes: int | None = None
        self.test_filter = _filter_config("test_cfg", "test")
        self.production_filter = _filter_config("prod_cfg", "production")

    def check_dependencies(self) -> list[DependencyHealth]:
        return self.dependencies

    def list_providers(self) -> ProvidersResponse:
        return ProvidersResponse(providers=self.providers, generated_at=_now())

    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        self.throughput_window = window
        self.throughput_start_at = start_at
        self.throughput_end_at = end_at
        return ThroughputResponse(
            window=window,
            granularity=_throughput_granularity_for_window(window),
            window_start_at=start_at or (_now() - timedelta(hours=1)),
            window_end_at=end_at or _now(),
            buckets=[],
            generated_at=_now(),
        )

    def get_thesis_builder_metrics(
        self,
        *,
        window: str,
        evidence_collection_max_minutes: int,
    ) -> ThesisBuilderMetricsResponse:
        self.thesis_builder_window = window
        self.thesis_builder_evidence_collection_max_minutes = evidence_collection_max_minutes
        return _thesis_builder_metrics(window=window)

    def get_backlog(self) -> BacklogResponse:
        return BacklogResponse(pending_count=0, retrying_count=0, dead_letter_count=0, generated_at=_now())

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        self.dead_letter_limit = limit
        self.dead_letter_offset = offset
        return DeadLetterResponse(items=[], limit=limit, offset=offset, generated_at=_now())

    def get_filter_quality_status(self) -> FilterQualityStatusResponse:
        return FilterQualityStatusResponse(
            running_run=self.running_filter_quality_run,
            last_run=self.last_filter_quality_run,
            generated_at=_now(),
        )

    def list_filter_quality_incorrectly_rejected(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyRejectedResponse:
        self.incorrectly_rejected_run_id = run_id
        return FilterQualityIncorrectlyRejectedResponse(
            run_id=run_id,
            items=[
                FilterQualityIncorrectlyRejectedItem(
                    assessment_id="fqa_1",
                    run_id=run_id,
                    article_id="article_1",
                    headline="Revenue outlook improves",
                    summary="The company raised its revenue outlook after stronger demand.",
                    url="https://example.test/article",
                    source="example",
                    published_at=_now(),
                    production_matched_article_id="article_accepted",
                    production_matched_article_headline="Revenue outlook improves after guidance reset",
                    production_matched_article_url="https://example.test/article-accepted",
                    production_matched_article_source="finnhub",
                    production_matched_article_published_at=_now(),
                    production_filter_outcome="accepted",
                    simulation_filter_outcome="rejected",
                    rejection_reason_code="rejected_not_relevant",
                    production_rejection_reason_code=None,
                    simulation_rejection_reason_code="rejected_not_relevant",
                    probable_cause="keyword_gap",
                    improvement_suggestion="Add revenue outlook language to include keywords.",
                    rationale="The article contains material financial guidance.",
                    classification_confidence=0.91,
                    suggestion_json={},
                    evaluated_at=_now(),
                )
            ],
            generated_at=_now(),
        )

    def list_filter_quality_incorrectly_accepted(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyAcceptedResponse:
        self.incorrectly_accepted_run_id = run_id
        return FilterQualityIncorrectlyAcceptedResponse(
            run_id=run_id,
            items=[
                FilterQualityIncorrectlyAcceptedItem(
                    assessment_id="fqa_a1",
                    run_id=run_id,
                    article_id="article_a1",
                    headline="Five retirement lessons from summer market swings",
                    summary="A personal finance column about retirement planning and volatility.",
                    url="https://example.test/article-a1",
                    source="example",
                    published_at=_now(),
                    production_filter_outcome="accepted",
                    simulation_filter_outcome="accepted",
                    probable_cause="low_value_noise",
                    improvement_suggestion="Tighten exclude keywords for retirement-planning content.",
                    rationale="The article is evergreen advice without a market-moving catalyst.",
                    classification_confidence=0.82,
                    suggestion_json={},
                    evaluated_at=_now(),
                )
            ],
            generated_at=_now(),
        )

    def get_running_filter_quality_run(self) -> FilterQualityRunSummary | None:
        return self.running_filter_quality_run

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int:
        self.stale_timeout_seconds = timeout_seconds
        return 0

    def get_production_filter_config(self) -> NewsFilterConfigPayload:
        return self.production_filter

    def get_test_filter_config(self) -> NewsFilterConfigPayload:
        return self.test_filter

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        self.test_filter = payload
        return payload

    def promote_test_filter_config(self) -> NewsFilterConfigPayload:
        self.production_filter = self.test_filter.model_copy(
            update={"config_role": "production", "filter_config_id": "prod_promoted"}
        )
        return self.production_filter


class FailingProviderDataSource(FakeDataSource):
    def list_providers(self) -> ProvidersResponse:
        raise psycopg.OperationalError("database unavailable")


class FailingThroughputDataSource(FakeDataSource):
    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        raise psycopg.OperationalError("database unavailable")


class FailingBacklogDataSource(FakeDataSource):
    def get_backlog(self) -> BacklogResponse:
        raise redis.ConnectionError("redis unavailable")


class FailingDeadLettersDataSource(FakeDataSource):
    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        raise psycopg.OperationalError("database unavailable")


class FailingFilterQualityStatusDataSource(FakeDataSource):
    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int:
        raise psycopg.OperationalError("database unavailable")


class FakeFilterQualityRunner:
    def __init__(self, run_id: str = "fqe_started") -> None:
        self.run_id = run_id
        self.starts = 0
        self.last_accepted_audit_enabled = False

    def start_last_24h_run(self, *, accepted_audit_enabled: bool = False) -> str:
        self.starts += 1
        self.last_accepted_audit_enabled = accepted_audit_enabled
        return self.run_id

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str:
        self.starts += 1
        return self.run_id


class FakeWatchlistAdmin:
    def __init__(self) -> None:
        self.items = [
            SharedWatchlistRecord(
                ticker="AAPL",
                exchange_code="XNAS",
                display_name="Apple Inc",
                aliases=("apple",),
                is_active=True,
                source="manual",
            )
        ]
        self.lookup_query: str | None = None
        self.lookup_cached = False
        self.alias_cached = False
        self.alias_discovery_result: InstrumentLookupSuggestion | None = InstrumentLookupSuggestion(
            ticker="NVDA",
            exchange_code="XNAS",
            display_name="NVIDIA Corporation",
            aliases=("nvidia", "nvidia corporation"),
            provider="massive",
        )
        self.deactivated: tuple[str, str] | None = None

    def list_watchlist(self) -> list[SharedWatchlistRecord]:
        return self.items

    def lookup(self, query: str):
        self.lookup_query = query
        return (
            [
                InstrumentLookupSuggestion(
                    ticker="NVDA",
                    exchange_code="XNAS",
                    display_name="NVIDIA Corporation",
                    aliases=("nvidia", "nvidia corporation"),
                    provider="massive",
                )
            ],
            self.lookup_cached,
        )

    def discover_aliases(self, *, ticker: str, exchange_code: str, display_name: str | None):
        return self.alias_discovery_result, self.alias_cached

    def add_watchlist_entry(self, entry: SharedWatchlistEntryInput) -> SharedWatchlistRecord:
        if any(item.ticker == entry.ticker and item.exchange_code == entry.exchange_code and item.is_active for item in self.items):
            raise DuplicateActiveWatchlistEntry("duplicate")
        record = SharedWatchlistRecord(
            ticker=entry.ticker,
            exchange_code=entry.exchange_code,
            display_name=entry.display_name,
            aliases=tuple(alias.lower() for alias in entry.aliases),
            is_active=True,
            source=entry.source,
        )
        self.items.append(record)
        return record

    def update_watchlist_entry(self, entry: SharedWatchlistEntryInput) -> SharedWatchlistRecord:
        record = SharedWatchlistRecord(
            ticker=entry.ticker,
            exchange_code=entry.exchange_code,
            display_name=entry.display_name,
            aliases=tuple(alias.lower() for alias in entry.aliases),
            is_active=True,
            source=entry.source,
        )
        self.items = [
            item for item in self.items if not (item.ticker == entry.ticker and item.exchange_code == entry.exchange_code)
        ] + [record]
        return record

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str) -> None:
        self.deactivated = (ticker, exchange_code)


def test_health_is_healthy_when_dependencies_are_up_and_provider_data_is_fresh() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "healthy"
    assert health.liveness == "healthy"
    assert health.stale_data is False


def test_watchlist_lookup_and_alias_discovery_are_delegated_to_shared_admin() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    watchlist_admin = FakeWatchlistAdmin()
    service = MonitoringService(settings=_settings(), data_source=data_source, watchlist_admin=watchlist_admin)

    lookup = service.lookup_watchlist_candidates(query="nvidia")
    aliases = service.discover_watchlist_aliases(ticker="NVDA", exchange_code="XNAS")

    assert watchlist_admin.lookup_query == "nvidia"
    assert lookup.suggestions[0].ticker == "NVDA"
    assert lookup.lookup_providers_configured is False
    assert lookup.lookup_message is not None
    assert aliases.found is True
    assert aliases.aliases == ["nvidia", "nvidia corporation"]


def test_watchlist_list_reports_lookup_provider_configuration_state() -> None:
    data_source = FakeDataSource(
        dependencies=[],
        providers=[],
    )
    watchlist_admin = FakeWatchlistAdmin()
    service = MonitoringService(settings=_settings(), data_source=data_source, watchlist_admin=watchlist_admin)

    response = service.list_watchlist()

    assert response.lookup_providers_configured is False
    assert response.lookup_message is not None


def test_watchlist_add_update_and_deactivate_work() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    watchlist_admin = FakeWatchlistAdmin()
    service = MonitoringService(settings=_settings(), data_source=data_source, watchlist_admin=watchlist_admin)

    created = service.add_watchlist_entry(
        WatchlistItemPayload(
            ticker="NVDA",
            exchange_code="XNAS",
            display_name="NVIDIA Corporation",
            aliases=["NVIDIA", "NVIDIA Corporation"],
            source="manual",
        )
    )
    updated = service.update_watchlist_entry(
        ticker="NVDA",
        exchange_code="XNAS",
        payload=WatchlistItemPayload(
            ticker="ignored",
            exchange_code="ignored",
            display_name="NVIDIA Corp",
            aliases=["NVIDIA"],
            source="manual",
        ),
    )
    service.deactivate_watchlist_entry(ticker="NVDA", exchange_code="XNAS")

    assert created.ticker == "NVDA"
    assert updated.display_name == "NVIDIA Corp"
    assert watchlist_admin.deactivated == ("NVDA", "XNAS")


def test_health_marks_readiness_unhealthy_when_dependency_fails() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="unhealthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "unhealthy"
    assert health.liveness == "healthy"
    assert health.active_incident_count == 1


def test_health_marks_liveness_unhealthy_when_provider_data_is_stale() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now() - timedelta(minutes=10))],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "healthy"
    assert health.liveness == "unhealthy"
    assert health.stale_data is True


def test_health_degrades_when_provider_telemetry_query_fails() -> None:
    data_source = FailingProviderDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="unhealthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "unhealthy"
    assert health.liveness == "unhealthy"
    assert health.stale_data is True


def test_list_providers_returns_degraded_response_when_dependency_is_unavailable() -> None:
    data_source = FailingProviderDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.list_providers()

    assert response.available is False
    assert response.message == "Provider telemetry unavailable."
    assert response.providers == []


def test_dead_letter_query_bounds_limit_and_offset() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    service.list_dead_letters(limit=1000, offset=-10)

    assert data_source.dead_letter_limit == 25
    assert data_source.dead_letter_offset == 0


def test_get_backlog_returns_degraded_response_when_dependency_is_unavailable() -> None:
    data_source = FailingBacklogDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_backlog()

    assert response.available is False
    assert response.message == "Backlog data unavailable."
    assert response.dead_letter_count == 0


def test_list_dead_letters_returns_degraded_response_when_dependency_is_unavailable() -> None:
    data_source = FailingDeadLettersDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.list_dead_letters(limit=25, offset=0)

    assert response.available is False
    assert response.message == "Dead-letter data unavailable."
    assert response.items == []


def test_get_throughput_uses_default_window_when_not_provided() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window=None)

    assert data_source.throughput_window == "1d"
    assert data_source.throughput_start_at is None
    assert data_source.throughput_end_at is None
    assert response.window == "1d"


def test_get_throughput_returns_degraded_response_when_dependency_is_unavailable() -> None:
    data_source = FailingThroughputDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="1d")

    assert response.available is False
    assert response.message == "Throughput data unavailable."
    assert response.window == "1d"
    assert response.buckets == []


def test_get_throughput_forwards_supported_preset_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="1d")

    assert data_source.throughput_window == "1d"
    assert response.window == "1d"
    assert response.granularity == "hour"


def test_get_throughput_accepts_7d_preset_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="7d")

    assert data_source.throughput_window == "7d"
    assert response.window == "7d"
    assert response.granularity == "hour"


def test_get_throughput_accepts_30d_preset_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="30d")

    assert data_source.throughput_window == "30d"
    assert response.window == "30d"
    assert response.granularity == "day"


def test_get_throughput_accepts_15m_preset_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="15m")

    assert data_source.throughput_window == "15m"
    assert response.window == "15m"
    assert response.granularity == "raw"


def test_get_throughput_accepts_1h_preset_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_throughput(window="1h")

    assert data_source.throughput_window == "1h"
    assert response.window == "1h"
    assert response.granularity == "raw"


def test_get_throughput_accepts_custom_range() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)
    start_at = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)
    end_at = datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)

    response = service.get_throughput(window="7d", start_at=start_at, end_at=end_at)

    assert data_source.throughput_window == "7d"
    assert data_source.throughput_start_at == start_at
    assert data_source.throughput_end_at == end_at
    assert response.window == "custom"
    assert response.granularity == "hour"


def test_throughput_granularity_matches_window_defaults() -> None:
    assert _throughput_granularity_for_window("15m") == "raw"
    assert _throughput_granularity_for_window("1h") == "raw"
    assert _throughput_granularity_for_window("1d") == "hour"
    assert _throughput_granularity_for_window("7d") == "hour"
    assert _throughput_granularity_for_window("30d") == "day"


def test_throughput_bucket_expression_matches_granularity() -> None:
    assert _throughput_bucket_expression("15m") == "created_at"
    assert _throughput_bucket_expression("1h") == "created_at"
    assert _throughput_bucket_expression("1d") == "date_trunc('hour', created_at)"
    assert _throughput_bucket_expression("7d") == "date_trunc('hour', created_at)"
    assert _throughput_bucket_expression("30d") == "date_trunc('day', created_at)"


def test_get_throughput_rejects_invalid_custom_range() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)
    start_at = datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)
    end_at = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)

    try:
        service.get_throughput(window=None, start_at=start_at, end_at=end_at)
    except InvalidThroughputWindow as exc:
        assert "start_at earlier than end_at" in str(exc)
    else:
        raise AssertionError("expected InvalidThroughputWindow")


def test_get_throughput_rejects_invalid_window_token() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    try:
        service.get_throughput(window="30m")
    except InvalidThroughputWindow as exc:
        assert "Unsupported throughput window" in str(exc)
    else:
        raise AssertionError("expected InvalidThroughputWindow")


def test_get_thesis_builder_metrics_forwards_supported_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.get_thesis_builder_metrics(window="7d")

    assert data_source.thesis_builder_window == "7d"
    assert data_source.thesis_builder_evidence_collection_max_minutes == 120
    assert response.window == "7d"
    assert response.created_thesis_cards_count == 2


def test_get_thesis_builder_metrics_rejects_invalid_window() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    try:
        service.get_thesis_builder_metrics(window="12h")
    except InvalidThroughputWindow as exc:
        assert "Unsupported thesis-builder metrics window" in str(exc)
    else:
        raise AssertionError("expected InvalidThroughputWindow")


def test_repository_maps_thesis_builder_metric_aggregates(monkeypatch) -> None:
    repository = PostgresRedisMonitoringDataSource(
        dsn="",
        news_schema="news_fetcher",
        filter_quality_schema="filter_quality_evaluator",
        thesis_builder_schema="thesis_builder",
        queue_url="redis://localhost:6379/0",
        news_raw_queue="news_raw_queue",
        query_timeout_seconds=1,
    )
    fetches = [
        {
            "articles_processed_count": 5,
            "market_moving_articles_count": 3,
        },
        {
            "created_thesis_cards_count": 2,
            "missed_stale_thesis_cards_count": 1,
            "stale_evidence_exceeded_avg_seconds": 300.0,
            "stale_evidence_exceeded_p95_seconds": 540.0,
            "stale_evidence_exceeded_max_seconds": 600.0,
        },
        {
            "pending_thesis_cards_count": 2,
            "oldest_pending_age_seconds": 720.0,
            "average_pending_age_seconds": 420.0,
            "minimum_pending_expires_in_seconds": 1200.0,
            "average_pending_expires_in_seconds": 1800.0,
        },
    ]

    monkeypatch.setattr(repository, "_fetch_one", lambda *_args, **_kwargs: fetches.pop(0))
    monkeypatch.setattr(repository, "_scalar_count", lambda *_args, **_kwargs: 4)
    monkeypatch.setattr(
        repository,
        "_fetch_pending_thesis_windows",
        lambda **_kwargs: [
            ThesisBuilderPendingWindow(
                window_id=9,
                ticker="AAPL",
                exchange_code="XNAS",
                strategy="event_driven",
                direction="buy",
                window_started_at=_now(),
                last_evidence_at=_now(),
                pending_age_seconds=720.0,
                expires_in_seconds=1200.0,
            )
        ],
    )

    response = repository.get_thesis_builder_metrics(window="1d", evidence_collection_max_minutes=120)

    assert response.available is True
    assert response.articles_processed_count == 5
    assert response.market_moving_articles_count == 3
    assert response.articles_included_in_cards_count == 4
    assert response.stale_articles_count == 4
    assert response.created_thesis_cards_count == 2
    assert response.pending_thesis_cards_count == 2
    assert response.oldest_pending_age_seconds == 720.0
    assert response.missed_stale_thesis_cards_count == 1
    assert response.stale_evidence_exceeded_p95_seconds == 540.0
    assert response.pending_windows[0].ticker == "AAPL"


def test_filter_quality_status_returns_running_and_last_terminal_run() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    data_source.running_filter_quality_run = _filter_quality_run("fqe_running", "running")
    data_source.last_filter_quality_run = _filter_quality_run("fqe_done", "completed")
    service = MonitoringService(settings=_settings(), data_source=data_source)

    status = service.get_filter_quality_status()

    assert data_source.stale_timeout_seconds == 1800
    assert status.running_run is not None
    assert status.running_run.run_id == "fqe_running"
    assert status.last_run is not None
    assert status.last_run.run_id == "fqe_done"


def test_filter_quality_status_returns_degraded_response_when_dependency_is_unavailable() -> None:
    data_source = FailingFilterQualityStatusDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    status = service.get_filter_quality_status()

    assert status.available is False
    assert status.message == "Filter-quality data unavailable."
    assert status.running_run is None
    assert status.last_run is None


def test_list_filter_quality_incorrectly_rejected_delegates_to_data_source() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.list_filter_quality_incorrectly_rejected(run_id="fqe_done")

    assert data_source.incorrectly_rejected_run_id == "fqe_done"
    assert response.run_id == "fqe_done"
    assert response.items[0].probable_cause == "keyword_gap"
    assert response.items[0].improvement_suggestion == "Add revenue outlook language to include keywords."
    assert response.items[0].production_matched_article_id == "article_accepted"
    assert response.items[0].summary == "The company raised its revenue outlook after stronger demand."


def test_list_filter_quality_incorrectly_accepted_delegates_to_data_source() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.list_filter_quality_incorrectly_accepted(run_id="fqe_done")

    assert data_source.incorrectly_accepted_run_id == "fqe_done"
    assert response.run_id == "fqe_done"
    assert response.items[0].probable_cause == "low_value_noise"
    assert response.items[0].improvement_suggestion == "Tighten exclude keywords for retirement-planning content."
    assert response.items[0].summary == "A personal finance column about retirement planning and volatility."


def test_incorrectly_rejected_item_sanitizes_legacy_keyword_recommendations() -> None:
    item = _incorrectly_rejected_item(
        {
            "assessment_id": "fqa_1",
            "run_id": "fqe_done",
            "article_id": "a1",
            "headline": "How exploding investor euphoria and leveraged ETFs turned one stock-market bull cautious",
            "summary": "A Barclays strategist explains why it is time to turn cautious on U.S. stocks.",
            "url": "https://example.com/a1",
            "source": "rss",
            "published_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
            "production_matched_article_id": "a2",
            "production_matched_article_headline": "How investor euphoria turned one stock-market bull cautious",
            "production_matched_article_url": "https://example.com/a2",
            "production_matched_article_source": "rss",
            "production_matched_article_published_at": datetime(2026, 6, 10, 1, tzinfo=timezone.utc),
            "production_filter_outcome": "rejected",
            "simulation_filter_outcome": "rejected",
            "rejection_reason_code": "rejected_not_relevant",
            "production_rejection_reason_code": "rejected_soft_duplicate",
            "simulation_rejection_reason_code": "rejected_not_relevant",
            "probable_cause": "keyword_gap",
            "improvement_suggestion": "Add market phrases.",
            "rationale": "Market-wide catalyst.",
            "classification_confidence": 0.85,
            "suggestion_json": {
                "recommended_include_keywords": ["investor sentiment", "leveraged ETFs", "stock-market bull"]
            },
            "filter_config_snapshot_json": {"include_keywords": ["bull"]},
            "evaluated_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
        }
    )

    assert item.recommended_include_keywords == ["leveraged etfs"]
    assert item.suggestion_json["recommended_include_keywords"] == ["leveraged etfs"]
    assert item.production_rejection_reason_code == "rejected_soft_duplicate"
    assert item.simulation_rejection_reason_code == "rejected_not_relevant"
    assert item.production_matched_article_headline == "How investor euphoria turned one stock-market bull cautious"


def test_incorrectly_accepted_item_preserves_rationale_and_suggestion() -> None:
    item = _incorrectly_accepted_item(
        {
            "assessment_id": "fqa_a1",
            "run_id": "fqe_done",
            "article_id": "a1",
            "headline": "Five retirement lessons from summer market swings",
            "summary": "A personal finance article with no catalyst.",
            "url": "https://example.com/a1",
            "source": "rss",
            "published_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
            "production_filter_outcome": "accepted",
            "simulation_filter_outcome": "accepted",
            "probable_cause": "low_value_noise",
            "improvement_suggestion": "Tighten exclude keywords.",
            "rationale": "The article is evergreen advice.",
            "classification_confidence": 0.73,
            "suggestion_json": {"review_tags": ["retirement"]},
            "evaluated_at": datetime(2026, 6, 10, tzinfo=timezone.utc),
        }
    )

    assert item.probable_cause == "low_value_noise"
    assert item.improvement_suggestion == "Tighten exclude keywords."
    assert item.rationale == "The article is evergreen advice."
    assert item.suggestion_json["review_tags"] == ["retirement"]


def test_start_filter_quality_run_returns_running_when_no_active_run_exists() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    runner = FakeFilterQualityRunner(run_id="fqe_new")
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=runner)

    response = service.start_filter_quality_run()

    assert response.run_id == "fqe_new"
    assert response.status == "running"
    assert runner.starts == 1
    assert runner.last_accepted_audit_enabled is False
    assert data_source.stale_timeout_seconds == 1800


def test_start_filter_quality_run_can_enable_accepted_audit() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    runner = FakeFilterQualityRunner(run_id="fqe_new")
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=runner)

    response = service.start_filter_quality_run(
        FilterQualityStartRunRequest(accepted_audit_enabled=True)
    )

    assert response.run_id == "fqe_new"
    assert response.status == "running"
    assert runner.starts == 1
    assert runner.last_accepted_audit_enabled is True


def test_start_filter_quality_run_raises_when_run_is_already_running() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    data_source.running_filter_quality_run = _filter_quality_run("fqe_active", "running")
    runner = FakeFilterQualityRunner()
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=runner)

    try:
        service.start_filter_quality_run()
    except FilterQualityRunAlreadyActive as exc:
        assert exc.run_id == "fqe_active"
    else:
        raise AssertionError("expected FilterQualityRunAlreadyActive")
    assert runner.starts == 0


def test_filter_config_workflow_delegates_to_data_source() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=FakeFilterQualityRunner())

    test_filter = service.get_test_filter_config()
    saved = service.save_test_filter_config(
        test_filter.model_copy(
            update={
                "include_keywords": ["guidance", "outlook"],
                "created_from_run_id": "fqe_done",
            }
        )
    )
    simulation = service.start_test_filter_simulation()
    production = service.promote_test_filter_config()

    assert saved.include_keywords == ["guidance", "outlook"]
    assert saved.created_from_run_id == "fqe_done"
    assert simulation.status == "running"
    assert production.config_role == "production"


def test_filter_quality_coordinator_builds_last_24h_params_with_accepted_audit_disabled_by_default() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    coordinator = FilterQualityRunCoordinator(
        settings_factory=lambda: type("Settings", (), {"accepted_audit_sample_size": 25})(),
        run_id_factory=lambda: "fqe_ui",
        now_factory=lambda: now,
    )

    params = coordinator._build_last_24h_params()

    assert params.run_id == "fqe_ui"
    assert params.news_window_start_at == now - timedelta(hours=24)
    assert params.news_window_end_at == now
    assert params.accepted_audit_enabled is False
    assert params.accepted_audit_sample_size is None
    assert params.run_note == "Started from Monitoring UI"


def test_filter_quality_coordinator_builds_last_24h_params_with_accepted_audit_when_requested() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    coordinator = FilterQualityRunCoordinator(
        settings_factory=lambda: type("Settings", (), {"accepted_audit_sample_size": 25})(),
        run_id_factory=lambda: "fqe_ui",
        now_factory=lambda: now,
    )

    params = coordinator._build_last_24h_params(accepted_audit_enabled=True)

    assert params.run_id == "fqe_ui"
    assert params.news_window_start_at == now - timedelta(hours=24)
    assert params.news_window_end_at == now
    assert params.accepted_audit_enabled is True
    assert params.accepted_audit_sample_size == 25
    assert params.run_note == "Started from Monitoring UI"


def _settings() -> MonitoringUiSettings:
    return MonitoringUiSettings(
        ui_port=8080,
        ui_api_base_url="http://localhost:8080/api",
        ui_refresh_interval_seconds=15,
        ui_provider_refresh_interval_seconds=10,
        ui_alerts_refresh_interval_seconds=20,
        ui_query_timeout_seconds=5,
        ui_stale_data_ttl_seconds=120,
        ui_default_time_window="1d",
        ui_export_max_rows=25,
        newsfetcher_db_schema="news_fetcher",
        filter_quality_db_schema="filter_quality_evaluator",
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        watchlist_table="t_watchlist_tickers",
        thesis_builder_evidence_collection_max_minutes=120,
        filter_quality_run_timeout_seconds=1800,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        massive_api_key="",
        massive_api_base_url="https://api.polygon.io",
        alpha_vantage_api_key="",
        instrument_lookup_cache_ttl_seconds=21600,
        instrument_alias_cache_ttl_seconds=86400,
        instrument_lookup_provider_debounce_ms=300,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _filter_quality_run(run_id: str, status: str) -> FilterQualityRunSummary:
    return FilterQualityRunSummary(
        run_id=run_id,
        status=status,
        news_window_start_at=_now() - timedelta(hours=24),
        news_window_end_at=_now(),
        created_at=_now(),
        started_at=_now(),
        finished_at=_now() if status != "running" else None,
        error_code=None,
        rejection_precision_proxy=0.9,
        incorrectly_accepted_rate_estimate=None,
        dataset_input_count=12,
        dataset_rejected_count=7,
        dataset_accepted_count=5,
        rejected_items_evaluated=7,
        accepted_items_sampled=0,
        correctly_rejected_count=6,
        incorrectly_rejected_count=1,
        correctly_accepted_count=0,
        incorrectly_accepted_count=0,
        item_failed_count=0,
        item_error_codes={},
        total_filter_quality=0.9,
        total_correct_count=11,
        assumed_correct_accepted_count=5,
        evaluation_subject="simulation",
        summary_json={},
        recommendation_summary_md="",
    )


def _filter_config(config_id: str, role: str) -> NewsFilterConfigPayload:
    return NewsFilterConfigPayload(
        filter_config_id=config_id,
        config_name=f"{role} filter",
        config_role=role,
        status="active",
        include_keywords=["guidance"],
        exclude_keywords=[],
        watchlist_tickers=["AAPL"],
        dedupe_algorithm="rapidfuzz_ratio",
        dedupe_similarity_threshold=0.9,
        dedupe_lookback_hours=24,
    )


def _thesis_builder_metrics(window: str = "1d") -> ThesisBuilderMetricsResponse:
    return ThesisBuilderMetricsResponse(
        available=True,
        window=window,
        window_start_at=_now(),
        window_end_at=_now(),
        articles_processed_count=5,
        market_moving_articles_count=3,
        articles_included_in_cards_count=4,
        stale_articles_count=1,
        created_thesis_cards_count=2,
        pending_thesis_cards_count=1,
        oldest_pending_age_seconds=600.0,
        average_pending_age_seconds=300.0,
        minimum_pending_expires_in_seconds=1200.0,
        average_pending_expires_in_seconds=1200.0,
        missed_stale_thesis_cards_count=1,
        stale_evidence_exceeded_avg_seconds=300.0,
        stale_evidence_exceeded_p95_seconds=540.0,
        stale_evidence_exceeded_max_seconds=600.0,
        pending_windows=[
            ThesisBuilderPendingWindow(
                window_id=1,
                ticker="AAPL",
                exchange_code="XNAS",
                strategy="event_driven",
                direction="buy",
                window_started_at=_now(),
                last_evidence_at=_now(),
                pending_age_seconds=600.0,
                expires_in_seconds=1200.0,
            )
        ],
        generated_at=_now(),
    )
