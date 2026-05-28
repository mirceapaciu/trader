from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import (
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    HealthResponse,
    ProvidersResponse,
    ThroughputResponse,
)
from .settings import MonitoringUiSettings


class MonitoringDataSource(Protocol):
    def check_dependencies(self) -> list[DependencyHealth]: ...

    def list_providers(self) -> ProvidersResponse: ...

    def get_throughput(self, *, window: str) -> ThroughputResponse: ...

    def get_backlog(self) -> BacklogResponse: ...

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse: ...


class MonitoringService:
    def __init__(self, *, settings: MonitoringUiSettings, data_source: MonitoringDataSource) -> None:
        self._settings = settings
        self._data_source = data_source

    def get_health(self) -> HealthResponse:
        now = _utc_now()
        dependencies = self._data_source.check_dependencies()
        try:
            providers = self._data_source.list_providers()
        except Exception:
            providers = ProvidersResponse(providers=[], generated_at=now)

        postgres_ok = _dependency_healthy(dependencies, "postgres")
        redis_ok = _dependency_healthy(dependencies, "redis")
        latest_cycle = max(
            (provider.last_cycle_end_at for provider in providers.providers if provider.last_cycle_end_at),
            default=None,
        )
        stale_cutoff = now - timedelta(seconds=self._settings.ui_stale_data_ttl_seconds)
        stale_data = latest_cycle is None or latest_cycle < stale_cutoff

        readiness = "healthy" if postgres_ok and redis_ok else "unhealthy"
        liveness = "healthy" if not stale_data else "unhealthy"
        incident_count = sum(1 for dependency in dependencies if dependency.state == "unhealthy")

        return HealthResponse(
            readiness=readiness,
            liveness=liveness,
            stale_data=stale_data,
            last_successful_refresh_at=latest_cycle or now,
            dependencies=dependencies,
            active_incident_count=incident_count,
        )

    def list_providers(self) -> ProvidersResponse:
        return self._data_source.list_providers()

    def get_throughput(self, *, window: str | None) -> ThroughputResponse:
        return self._data_source.get_throughput(window=window or self._settings.ui_default_time_window)

    def get_backlog(self) -> BacklogResponse:
        return self._data_source.get_backlog()

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        bounded_limit = max(1, min(limit, self._settings.ui_export_max_rows))
        bounded_offset = max(0, offset)
        return self._data_source.list_dead_letters(limit=bounded_limit, offset=bounded_offset)


def _dependency_healthy(dependencies: list[DependencyHealth], kind: str) -> bool:
    return any(dependency.kind == kind and dependency.state == "healthy" for dependency in dependencies)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
