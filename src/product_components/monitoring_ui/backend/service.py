from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg
import redis

from src.product_components.shared.adapters import SharedWatchlistEntryInput
from src.product_components.shared.instrument_lookup import (
    DuplicateActiveWatchlistEntry,
    InstrumentLookupSuggestion,
    SharedInstrumentLookupAdminService,
)

from .models import (
    AliasDiscoveryResponse,
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunRequest,
    FilterQualityStartRunResponse,
    FilterQualityStatusResponse,
    FilterConfigSimulationStartResponse,
    HealthResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ThesisBuilderMetricsResponse,
    ThroughputGranularity,
    ThroughputResponse,
    WatchlistItemPayload,
    WatchlistItemResponse,
    WatchlistLookupResponse,
    WatchlistLookupSuggestionResponse,
    WatchlistResponse,
)
from .settings import MonitoringUiSettings

_INFRASTRUCTURE_ERRORS = (psycopg.Error, redis.RedisError, TimeoutError)


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

    def get_thesis_builder_metrics(
        self,
        *,
        window: str,
        evidence_collection_max_minutes: int,
    ) -> ThesisBuilderMetricsResponse: ...

    def get_backlog(self) -> BacklogResponse: ...

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse: ...

    def get_filter_quality_status(self) -> FilterQualityStatusResponse: ...

    def list_filter_quality_incorrectly_rejected(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyRejectedResponse: ...

    def list_filter_quality_incorrectly_accepted(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyAcceptedResponse: ...

    def get_production_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload: ...

    def promote_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_running_filter_quality_run(self): ...

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int: ...


class FilterQualityRunner(Protocol):
    def start_last_24h_run(self, *, accepted_audit_enabled: bool = False) -> str: ...

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
        watchlist_admin: SharedInstrumentLookupAdminService | None = None,
    ) -> None:
        self._settings = settings
        self._data_source = data_source
        self._filter_quality_runner = filter_quality_runner
        self._watchlist_admin = watchlist_admin

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
        try:
            return self._data_source.list_providers()
        except _INFRASTRUCTURE_ERRORS:
            return ProvidersResponse(
                available=False,
                message="Provider telemetry unavailable.",
                providers=[],
                generated_at=_utc_now(),
            )

    def get_throughput(
        self,
        *,
        window: str | None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported throughput window: {selected_window}")
        if start_at is not None or end_at is not None:
            if start_at is None or end_at is None:
                raise InvalidThroughputWindow("Custom throughput ranges require both start_at and end_at.")
            normalized_start = _to_utc(start_at)
            normalized_end = _to_utc(end_at)
            if normalized_start >= normalized_end:
                raise InvalidThroughputWindow("Custom throughput ranges must have start_at earlier than end_at.")
            if normalized_end - normalized_start > timedelta(days=30):
                raise InvalidThroughputWindow("Custom throughput ranges must not exceed 30 days.")
            try:
                response = self._data_source.get_throughput(
                    window=selected_window,
                    start_at=normalized_start,
                    end_at=normalized_end,
                )
            except _INFRASTRUCTURE_ERRORS:
                return _unavailable_throughput_response(
                    window="custom",
                    selected_window=selected_window,
                    start_at=normalized_start,
                    end_at=normalized_end,
                    message="Throughput data unavailable.",
                )
            return response.model_copy(update={"window": "custom"})

        try:
            return self._data_source.get_throughput(window=selected_window)
        except _INFRASTRUCTURE_ERRORS:
            now = _utc_now()
            return _unavailable_throughput_response(
                window=selected_window,
                selected_window=selected_window,
                start_at=now - _window_duration(selected_window),
                end_at=now,
                message="Throughput data unavailable.",
            )

    def get_thesis_builder_metrics(self, *, window: str | None) -> ThesisBuilderMetricsResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported thesis-builder metrics window: {selected_window}")
        try:
            return self._data_source.get_thesis_builder_metrics(
                window=selected_window,
                evidence_collection_max_minutes=self._settings.thesis_builder_evidence_collection_max_minutes,
            )
        except _INFRASTRUCTURE_ERRORS:
            now = _utc_now()
            return ThesisBuilderMetricsResponse(
                available=False,
                message="ThesisBuilder telemetry unavailable.",
                window=selected_window,
                window_start_at=now - _window_duration(selected_window),
                window_end_at=now,
                articles_processed_count=0,
                market_moving_articles_count=0,
                articles_included_in_cards_count=0,
                stale_articles_count=0,
                created_thesis_cards_count=0,
                pending_thesis_cards_count=0,
                missed_stale_thesis_cards_count=0,
                generated_at=now,
            )

    def get_backlog(self) -> BacklogResponse:
        try:
            return self._data_source.get_backlog()
        except _INFRASTRUCTURE_ERRORS:
            return BacklogResponse(
                available=False,
                message="Backlog data unavailable.",
                pending_count=0,
                retrying_count=0,
                dead_letter_count=0,
                generated_at=_utc_now(),
            )

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        bounded_limit = max(1, min(limit, self._settings.ui_export_max_rows))
        bounded_offset = max(0, offset)
        try:
            return self._data_source.list_dead_letters(limit=bounded_limit, offset=bounded_offset)
        except _INFRASTRUCTURE_ERRORS:
            return DeadLetterResponse(
                available=False,
                message="Dead-letter data unavailable.",
                items=[],
                limit=bounded_limit,
                offset=bounded_offset,
                generated_at=_utc_now(),
            )

    def get_filter_quality_status(self) -> FilterQualityStatusResponse:
        try:
            self._data_source.mark_stale_filter_quality_runs_failed(
                timeout_seconds=self._settings.filter_quality_run_timeout_seconds,
            )
            return self._data_source.get_filter_quality_status()
        except _INFRASTRUCTURE_ERRORS:
            return FilterQualityStatusResponse(
                available=False,
                message="Filter-quality data unavailable.",
                running_run=None,
                last_run=None,
                generated_at=_utc_now(),
            )

    def list_filter_quality_incorrectly_rejected(self, *, run_id: str) -> FilterQualityIncorrectlyRejectedResponse:
        return self._data_source.list_filter_quality_incorrectly_rejected(run_id=run_id)

    def list_filter_quality_incorrectly_accepted(self, *, run_id: str) -> FilterQualityIncorrectlyAcceptedResponse:
        return self._data_source.list_filter_quality_incorrectly_accepted(run_id=run_id)

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

    def start_filter_quality_run(
        self,
        payload: FilterQualityStartRunRequest | None = None,
    ) -> FilterQualityStartRunResponse:
        if self._filter_quality_runner is None:
            raise RuntimeError("filter_quality_runner_unavailable")
        self._data_source.mark_stale_filter_quality_runs_failed(
            timeout_seconds=self._settings.filter_quality_run_timeout_seconds,
        )
        active_run = self._data_source.get_running_filter_quality_run()
        if active_run is not None:
            raise FilterQualityRunAlreadyActive(active_run.run_id)
        accepted_audit_enabled = payload.accepted_audit_enabled if payload is not None else False
        try:
            run_id = self._filter_quality_runner.start_last_24h_run(
                accepted_audit_enabled=accepted_audit_enabled
            )
        except FilterQualityRunAlreadyActive:
            raise
        return FilterQualityStartRunResponse(run_id=run_id, status="running")

    def list_watchlist(self) -> WatchlistResponse:
        admin = self._require_watchlist_admin()
        items = [
            WatchlistItemResponse(
                ticker=row.ticker,
                exchange_code=row.exchange_code,
                display_name=row.display_name,
                aliases=list(row.aliases),
                is_active=row.is_active,
                source=row.source,
                has_missing_aliases=row.has_missing_aliases,
            )
            for row in admin.list_watchlist()
        ]
        return WatchlistResponse(
            lookup_providers_configured=True,
            items=items,
            generated_at=_utc_now(),
        )

    def lookup_watchlist_candidates(self, *, query: str, expand: bool = False) -> WatchlistLookupResponse:
        admin = self._require_watchlist_admin()
        lookup_providers_configured = bool(
            self._settings.massive_api_key.strip() or self._settings.alpha_vantage_api_key.strip()
        ) or True  # OpenFIGI works without a key
        suggestions, cached, provider_warnings = admin.lookup(query, expand=expand)
        return WatchlistLookupResponse(
            query=query,
            lookup_providers_configured=lookup_providers_configured,
            suggestions=[_lookup_suggestion_response(item) for item in suggestions],
            cached=cached,
            provider_warnings=provider_warnings,
            generated_at=_utc_now(),
        )

    def add_watchlist_entry(self, payload: WatchlistItemPayload) -> WatchlistItemResponse:
        admin = self._require_watchlist_admin()
        try:
            row = admin.add_watchlist_entry(_watchlist_input(payload))
        except DuplicateActiveWatchlistEntry:
            raise
        return WatchlistItemResponse(
            ticker=row.ticker,
            exchange_code=row.exchange_code,
            display_name=row.display_name,
            aliases=list(row.aliases),
            is_active=row.is_active,
            source=row.source,
            has_missing_aliases=row.has_missing_aliases,
        )

    def update_watchlist_entry(
        self,
        *,
        ticker: str,
        exchange_code: str,
        payload: WatchlistItemPayload,
    ) -> WatchlistItemResponse:
        admin = self._require_watchlist_admin()
        row = admin.update_watchlist_entry(
            _watchlist_input(
                payload.model_copy(
                    update={
                        "ticker": ticker.strip().upper(),
                        "exchange_code": exchange_code.strip().upper(),
                    }
                )
            )
        )
        return WatchlistItemResponse(
            ticker=row.ticker,
            exchange_code=row.exchange_code,
            display_name=row.display_name,
            aliases=list(row.aliases),
            is_active=row.is_active,
            source=row.source,
            has_missing_aliases=row.has_missing_aliases,
        )

    def discover_watchlist_aliases(self, *, ticker: str, exchange_code: str) -> AliasDiscoveryResponse:
        admin = self._require_watchlist_admin()
        current = next(
            (
                item
                for item in admin.list_watchlist()
                if item.ticker == ticker.strip().upper() and item.exchange_code == exchange_code.strip().upper()
            ),
            None,
        )
        suggestion, cached = admin.discover_aliases(
            ticker=ticker,
            exchange_code=exchange_code,
            display_name=current.display_name if current is not None else None,
        )
        if suggestion is None:
            return AliasDiscoveryResponse(
                ticker=ticker.strip().upper(),
                exchange_code=exchange_code.strip().upper(),
                display_name=current.display_name if current is not None else None,
                aliases=[],
                provider=None,
                found=False,
                cached=cached,
                generated_at=_utc_now(),
            )
        return AliasDiscoveryResponse(
            ticker=suggestion.ticker,
            exchange_code=suggestion.exchange_code,
            display_name=suggestion.display_name,
            aliases=list(suggestion.aliases),
            provider=suggestion.provider,
            found=bool(suggestion.aliases),
            cached=cached,
            generated_at=_utc_now(),
        )

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str) -> None:
        admin = self._require_watchlist_admin()
        admin.deactivate_watchlist_entry(ticker=ticker, exchange_code=exchange_code)

    def _require_watchlist_admin(self) -> SharedInstrumentLookupAdminService:
        if self._watchlist_admin is None:
            raise RuntimeError("watchlist_admin_unavailable")
        return self._watchlist_admin


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


def _window_duration(window: str) -> timedelta:
    if window == "15m":
        return timedelta(minutes=15)
    if window == "1h":
        return timedelta(hours=1)
    if window == "1d":
        return timedelta(days=1)
    if window == "7d":
        return timedelta(days=7)
    if window == "30d":
        return timedelta(days=30)
    raise ValueError(f"Unsupported throughput window: {window}")


def _throughput_granularity(window: str) -> ThroughputGranularity:
    if window in {"15m", "1h"}:
        return "raw"
    if window in {"1d", "7d"}:
        return "hour"
    if window == "30d":
        return "day"
    raise ValueError(f"Unsupported throughput window: {window}")


def _unavailable_throughput_response(
    *,
    window: str,
    selected_window: str,
    start_at: datetime,
    end_at: datetime,
    message: str,
) -> ThroughputResponse:
    return ThroughputResponse(
        available=False,
        message=message,
        window=window,
        granularity=_throughput_granularity(selected_window),
        window_start_at=start_at,
        window_end_at=end_at,
        buckets=[],
        generated_at=_utc_now(),
    )


def _config_snapshot(config: NewsFilterConfigPayload) -> dict:
    return {
        "include_keywords": config.include_keywords,
        "exclude_keywords": config.exclude_keywords,
        "watchlist_tickers": config.watchlist_tickers,
        "dedupe_algorithm": config.dedupe_algorithm,
        "dedupe_similarity_threshold": config.dedupe_similarity_threshold,
        "dedupe_lookback_hours": config.dedupe_lookback_hours,
    }


def _watchlist_input(payload: WatchlistItemPayload) -> SharedWatchlistEntryInput:
    return SharedWatchlistEntryInput(
        ticker=payload.ticker.strip().upper(),
        exchange_code=payload.exchange_code.strip().upper(),
        display_name=payload.display_name.strip(),
        aliases=tuple(str(alias).strip() for alias in payload.aliases if str(alias).strip()),
        source=payload.source.strip() or "manual",
    )


def _lookup_suggestion_response(item: InstrumentLookupSuggestion) -> WatchlistLookupSuggestionResponse:
    return WatchlistLookupSuggestionResponse(
        ticker=item.ticker,
        exchange_code=item.exchange_code,
        display_name=item.display_name,
        aliases=list(item.aliases),
        provider=item.provider,
    )
