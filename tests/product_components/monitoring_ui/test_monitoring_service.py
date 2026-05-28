from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.monitoring_ui.backend.models import (
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    ProvidersResponse,
    ProviderStatus,
    ThroughputResponse,
)
from src.product_components.monitoring_ui.backend.service import MonitoringService
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


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

    def check_dependencies(self) -> list[DependencyHealth]:
        return self.dependencies

    def list_providers(self) -> ProvidersResponse:
        return ProvidersResponse(providers=self.providers, generated_at=_now())

    def get_throughput(self, *, window: str) -> ThroughputResponse:
        return ThroughputResponse(window=window, buckets=[], generated_at=_now())

    def get_backlog(self) -> BacklogResponse:
        return BacklogResponse(pending_count=0, retrying_count=0, dead_letter_count=0, generated_at=_now())

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        self.dead_letter_limit = limit
        self.dead_letter_offset = offset
        return DeadLetterResponse(items=[], limit=limit, offset=offset, generated_at=_now())


class FailingProviderDataSource(FakeDataSource):
    def list_providers(self) -> ProvidersResponse:
        raise RuntimeError("database unavailable")


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


def test_dead_letter_query_bounds_limit_and_offset() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    service.list_dead_letters(limit=1000, offset=-10)

    assert data_source.dead_letter_limit == 25
    assert data_source.dead_letter_offset == 0


def _settings() -> MonitoringUiSettings:
    return MonitoringUiSettings(
        ui_port=8080,
        ui_api_base_url="http://localhost:8080/api",
        ui_refresh_interval_seconds=15,
        ui_provider_refresh_interval_seconds=10,
        ui_alerts_refresh_interval_seconds=20,
        ui_query_timeout_seconds=5,
        ui_stale_data_ttl_seconds=120,
        ui_default_time_window="1h",
        ui_export_max_rows=25,
        newsfetcher_db_schema="news_fetcher",
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
