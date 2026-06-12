from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import (
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunResponse,
    FilterQualityStatusResponse,
    FilterConfigSimulationStartResponse,
    HealthResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ThroughputResponse,
)
from .settings import MonitoringUiSettings


class MonitoringDataSource(Protocol):
    def check_dependencies(self) -> list[DependencyHealth]: ...

    def list_providers(self) -> ProvidersResponse: ...

    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse: ...

    def get_backlog(self) -> BacklogResponse: ...

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse: ...

    def get_filter_quality_status(self) -> FilterQualityStatusResponse: ...

    def list_filter_quality_incorrectly_rejected(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyRejectedResponse: ...

    def get_production_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload: ...

    def promote_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_running_filter_quality_run(self): ...

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int: ...


class FilterQualityRunner(Protocol):
    def start_last_24h_run(self) -> str: ...

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str: ...


class FilterQualityRunAlreadyActive(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__("filter_quality_run_already_active")
        self.run_id = run_id


class InvalidThroughputWindow(ValueError):
    pass


class MonitoringService:
    def __init__(
        self,
        *,
        settings: MonitoringUiSettings,
        data_source: MonitoringDataSource,
        filter_quality_runner: FilterQualityRunner | None = None,
    ) -> None:
        self._settings = settings
        self._data_source = data_source
        self._filter_quality_runner = filter_quality_runner

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

    def get_throughput(
        self,
        *,
        window: str | None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        if window and (start_at is not None or end_at is not None):
            raise InvalidThroughputWindow("Specify either a preset window or start/end bounds, not both.")
        if start_at is not None or end_at is not None:
            if start_at is None or end_at is None:
                raise InvalidThroughputWindow("Custom throughput ranges require both start_at and end_at.")
            normalized_start = _to_utc(start_at)
            normalized_end = _to_utc(end_at)
            if normalized_start >= normalized_end:
                raise InvalidThroughputWindow("Custom throughput ranges must have start_at earlier than end_at.")
            if normalized_end - normalized_start > timedelta(days=7):
                raise InvalidThroughputWindow("Custom throughput ranges must not exceed 7 days.")
            return self._data_source.get_throughput(window="custom", start_at=normalized_start, end_at=normalized_end)

        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d"}:
            raise InvalidThroughputWindow(f"Unsupported throughput window: {selected_window}")
        return self._data_source.get_throughput(window=selected_window)

    def get_backlog(self) -> BacklogResponse:
        return self._data_source.get_backlog()

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        bounded_limit = max(1, min(limit, self._settings.ui_export_max_rows))
        bounded_offset = max(0, offset)
        return self._data_source.list_dead_letters(limit=bounded_limit, offset=bounded_offset)

    def get_filter_quality_status(self) -> FilterQualityStatusResponse:
        self._data_source.mark_stale_filter_quality_runs_failed(
            timeout_seconds=self._settings.filter_quality_run_timeout_seconds,
        )
        return self._data_source.get_filter_quality_status()

    def list_filter_quality_incorrectly_rejected(self, *, run_id: str) -> FilterQualityIncorrectlyRejectedResponse:
        return self._data_source.list_filter_quality_incorrectly_rejected(run_id=run_id)

    def get_production_filter_config(self) -> NewsFilterConfigPayload:
        return self._data_source.get_production_filter_config()

    def get_test_filter_config(self) -> NewsFilterConfigPayload:
        return self._data_source.get_test_filter_config()

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        return self._data_source.save_test_filter_config(payload)

    def start_test_filter_simulation(self) -> FilterConfigSimulationStartResponse:
        if self._filter_quality_runner is None:
            raise RuntimeError("filter_quality_runner_unavailable")
        self._data_source.mark_stale_filter_quality_runs_failed(
            timeout_seconds=self._settings.filter_quality_run_timeout_seconds,
        )
        active_run = self._data_source.get_running_filter_quality_run()
        if active_run is not None:
            raise FilterQualityRunAlreadyActive(active_run.run_id)
        test_filter = self._data_source.get_test_filter_config()
        run_id = self._filter_quality_runner.start_last_24h_run_with_snapshot(_config_snapshot(test_filter))
        return FilterConfigSimulationStartResponse(run_id=run_id, status="running")

    def promote_test_filter_config(self) -> NewsFilterConfigPayload:
        return self._data_source.promote_test_filter_config()

    def start_filter_quality_run(self) -> FilterQualityStartRunResponse:
        if self._filter_quality_runner is None:
            raise RuntimeError("filter_quality_runner_unavailable")
        self._data_source.mark_stale_filter_quality_runs_failed(
            timeout_seconds=self._settings.filter_quality_run_timeout_seconds,
        )
        active_run = self._data_source.get_running_filter_quality_run()
        if active_run is not None:
            raise FilterQualityRunAlreadyActive(active_run.run_id)
        try:
            run_id = self._filter_quality_runner.start_last_24h_run()
        except FilterQualityRunAlreadyActive:
            raise
        return FilterQualityStartRunResponse(run_id=run_id, status="running")


def _dependency_healthy(dependencies: list[DependencyHealth], kind: str) -> bool:
    return any(dependency.kind == kind and dependency.state == "healthy" for dependency in dependencies)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_throughput_window(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "24h":
        return "1d"
    return normalized


def _config_snapshot(config: NewsFilterConfigPayload) -> dict:
    return {
        "include_keywords": config.include_keywords,
        "exclude_keywords": config.exclude_keywords,
        "watchlist_tickers": config.watchlist_tickers,
        "dedupe_algorithm": config.dedupe_algorithm,
        "dedupe_similarity_threshold": config.dedupe_similarity_threshold,
        "dedupe_lookback_hours": config.dedupe_lookback_hours,
    }
