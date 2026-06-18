from __future__ import annotations

import re
from pathlib import Path

from datetime import datetime
from typing import Callable, TypeVar

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
import psycopg
import redis

from .filter_quality_runner import FilterQualityRunCoordinator
from .models import (
    AliasDiscoveryResponse,
    BacklogResponse,
    DeadLetterResponse,
    FilterConfigSimulationStartResponse,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunRequest,
    FilterQualityStartRunResponse,
    FilterQualityStatusResponse,
    HealthResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ThesisBuilderMetricsResponse,
    ThroughputResponse,
    WatchlistItemPayload,
    WatchlistItemResponse,
    WatchlistLookupResponse,
    WatchlistResponse,
)
from .repository import PostgresRedisMonitoringDataSource
from .service import FilterQualityRunAlreadyActive, InvalidThroughputWindow, MonitoringService
from .settings import MonitoringUiSettings
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
)
from src.product_components.shared.instrument_lookup import (
    AlphaVantageInstrumentLookupProvider,
    DuplicateActiveWatchlistEntry,
    MassiveInstrumentLookupProvider,
    OpenFigiInstrumentLookupProvider,
    SharedInstrumentLookupAdminService,
)

_INFRASTRUCTURE_ERRORS = (psycopg.Error, redis.RedisError, TimeoutError)
_T = TypeVar("_T")


def _local_dev_origin_regex() -> str:
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _run_with_infrastructure_mapping(operation: Callable[[], _T], *, detail: str) -> _T:
    try:
        return operation()
    except _INFRASTRUCTURE_ERRORS as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc


def create_app(settings: MonitoringUiSettings | None = None) -> FastAPI:
    resolved_settings = settings or MonitoringUiSettings.from_env()
    data_source = PostgresRedisMonitoringDataSource(
        dsn=resolved_settings.postgres_dsn,
        news_schema=resolved_settings.newsfetcher_db_schema,
        filter_quality_schema=resolved_settings.filter_quality_db_schema,
        thesis_builder_schema=resolved_settings.thesis_builder_db_schema,
        queue_url=resolved_settings.queue_url,
        news_raw_queue=resolved_settings.news_raw_queue,
        query_timeout_seconds=resolved_settings.ui_query_timeout_seconds,
    )
    try:
        data_source.bootstrap_shared_schema(repo_root=_repo_root())
        data_source.bootstrap_news_schema(repo_root=_repo_root())
        data_source.bootstrap_thesis_builder_schema(repo_root=_repo_root())
    except Exception:
        # The API still starts in degraded mode; dependency health reports database issues separately.
        pass
    watchlist_admin = SharedInstrumentLookupAdminService(
        registry=PostgresSharedInstrumentRegistry(
            dsn=resolved_settings.postgres_dsn,
            shared_schema=resolved_settings.shared_db_schema,
            watchlist_table=resolved_settings.watchlist_table,
        ),
        admin=PostgresSharedInstrumentAdmin(
            dsn=resolved_settings.postgres_dsn,
            shared_schema=resolved_settings.shared_db_schema,
        ),
        providers=(
            MassiveInstrumentLookupProvider(
                api_key=resolved_settings.massive_api_key,
                base_url=resolved_settings.massive_api_base_url,
            ),
            OpenFigiInstrumentLookupProvider(
                api_key=resolved_settings.openfigi_api_key,
            ),
            AlphaVantageInstrumentLookupProvider(
                api_key=resolved_settings.alpha_vantage_api_key,
            ),
        ),
        lookup_cache_ttl_seconds=resolved_settings.instrument_lookup_cache_ttl_seconds,
        alias_cache_ttl_seconds=resolved_settings.instrument_alias_cache_ttl_seconds,
        lookup_provider_debounce_ms=resolved_settings.instrument_lookup_provider_debounce_ms,
    )
    service = MonitoringService(
        settings=resolved_settings,
        data_source=data_source,
        filter_quality_runner=FilterQualityRunCoordinator(),
        watchlist_admin=watchlist_admin,
    )

    app = FastAPI(title="Trader Monitoring UI API")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_local_dev_origin_regex(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    def get_health() -> HealthResponse:
        return service.get_health()

    @app.get("/api/providers", response_model=ProvidersResponse)
    def list_providers() -> ProvidersResponse:
        return _run_with_infrastructure_mapping(
            service.list_providers,
            detail="provider telemetry unavailable",
        )

    @app.get("/api/metrics/throughput", response_model=ThroughputResponse)
    def get_throughput(
        window: str | None = Query(default=None),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
    ) -> ThroughputResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_throughput(window=window, start_at=start_at, end_at=end_at),
                detail="throughput data unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/thesis-builder/metrics", response_model=ThesisBuilderMetricsResponse)
    def get_thesis_builder_metrics(window: str | None = Query(default=None)) -> ThesisBuilderMetricsResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_thesis_builder_metrics(window=window),
                detail="thesis-builder metrics unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/backlog", response_model=BacklogResponse)
    def get_backlog() -> BacklogResponse:
        return _run_with_infrastructure_mapping(
            service.get_backlog,
            detail="backlog data unavailable",
        )

    @app.get("/api/dead-letter", response_model=DeadLetterResponse)
    def list_dead_letters(
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> DeadLetterResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.list_dead_letters(limit=limit, offset=offset),
            detail="dead-letter data unavailable",
        )

    @app.get("/api/filter-quality", response_model=FilterQualityStatusResponse)
    def get_filter_quality() -> FilterQualityStatusResponse:
        return _run_with_infrastructure_mapping(
            service.get_filter_quality_status,
            detail="filter-quality data unavailable",
        )

    @app.get(
        "/api/filter-quality/runs/{run_id}/incorrectly-rejected",
        response_model=FilterQualityIncorrectlyRejectedResponse,
    )
    def list_filter_quality_incorrectly_rejected(run_id: str) -> FilterQualityIncorrectlyRejectedResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.list_filter_quality_incorrectly_rejected(run_id=run_id),
            detail="filter-quality details unavailable",
        )

    @app.get(
        "/api/filter-quality/runs/{run_id}/incorrectly-accepted",
        response_model=FilterQualityIncorrectlyAcceptedResponse,
    )
    def list_filter_quality_incorrectly_accepted(run_id: str) -> FilterQualityIncorrectlyAcceptedResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.list_filter_quality_incorrectly_accepted(run_id=run_id),
            detail="filter-quality details unavailable",
        )

    @app.get("/api/filter-configs/production", response_model=NewsFilterConfigPayload)
    def get_production_filter_config() -> NewsFilterConfigPayload:
        return _run_with_infrastructure_mapping(
            service.get_production_filter_config,
            detail="production filter config unavailable",
        )

    @app.get("/api/filter-configs/test", response_model=NewsFilterConfigPayload)
    def get_test_filter_config() -> NewsFilterConfigPayload:
        return _run_with_infrastructure_mapping(
            service.get_test_filter_config,
            detail="test filter config unavailable",
        )

    @app.put("/api/filter-configs/test", response_model=NewsFilterConfigPayload)
    def save_test_filter_config(payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        return _run_with_infrastructure_mapping(
            lambda: service.save_test_filter_config(payload),
            detail="test filter config unavailable",
        )

    @app.post(
        "/api/filter-configs/test/simulations",
        response_model=FilterConfigSimulationStartResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_test_filter_simulation() -> FilterConfigSimulationStartResponse:
        try:
            return _run_with_infrastructure_mapping(
                service.start_test_filter_simulation,
                detail="filter-quality runner unavailable",
            )
        except FilterQualityRunAlreadyActive as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"run_id": exc.run_id, "status": "running", "message": "already running"},
            ) from exc

    @app.post("/api/filter-configs/test/promote", response_model=NewsFilterConfigPayload)
    def promote_test_filter_config() -> NewsFilterConfigPayload:
        return _run_with_infrastructure_mapping(
            service.promote_test_filter_config,
            detail="test filter config unavailable",
        )

    @app.post(
        "/api/filter-quality/runs",
        response_model=FilterQualityStartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_filter_quality_run(payload: FilterQualityStartRunRequest | None = None) -> FilterQualityStartRunResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.start_filter_quality_run(payload),
                detail="filter-quality runner unavailable",
            )
        except FilterQualityRunAlreadyActive as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"run_id": exc.run_id, "status": "running", "message": "already running"},
            ) from exc

    @app.post("/api/actions/refresh")
    def refresh() -> dict[str, str]:
        return {"status": "accepted"}

    @app.post("/api/actions/alert-test")
    def alert_test() -> dict[str, str]:
        return {"status": "accepted"}

    @app.get("/api/watchlist", response_model=WatchlistResponse)
    def list_watchlist() -> WatchlistResponse:
        return _run_with_infrastructure_mapping(
            service.list_watchlist,
            detail="watchlist data unavailable",
        )

    @app.get("/api/watchlist/lookups", response_model=WatchlistLookupResponse)
    def lookup_watchlist_candidates(
        query: str = Query(min_length=1),
        expand: bool = Query(default=False),
    ) -> WatchlistLookupResponse:
        if not query.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="lookup query must not be empty",
            )
        return _run_with_infrastructure_mapping(
            lambda: service.lookup_watchlist_candidates(query=query, expand=expand),
            detail="watchlist lookup unavailable",
        )

    @app.post("/api/watchlist", response_model=WatchlistItemResponse, status_code=status.HTTP_201_CREATED)
    def add_watchlist_entry(payload: WatchlistItemPayload) -> WatchlistItemResponse:
        try:
            return service.add_watchlist_entry(payload)
        except DuplicateActiveWatchlistEntry as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except _INFRASTRUCTURE_ERRORS as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="watchlist storage unavailable") from exc

    @app.put("/api/watchlist/{ticker}/{exchange_code}", response_model=WatchlistItemResponse)
    def update_watchlist_entry(
        ticker: str,
        exchange_code: str,
        payload: WatchlistItemPayload,
    ) -> WatchlistItemResponse:
        try:
            return service.update_watchlist_entry(
                ticker=ticker,
                exchange_code=exchange_code,
                payload=payload,
            )
        except _INFRASTRUCTURE_ERRORS as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="watchlist storage unavailable") from exc

    @app.post(
        "/api/watchlist/{ticker}/{exchange_code}/alias-discovery",
        response_model=AliasDiscoveryResponse,
    )
    def discover_watchlist_aliases(ticker: str, exchange_code: str) -> AliasDiscoveryResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.discover_watchlist_aliases(ticker=ticker, exchange_code=exchange_code),
            detail="watchlist alias discovery unavailable",
        )

    @app.delete("/api/watchlist/{ticker}/{exchange_code}", status_code=status.HTTP_204_NO_CONTENT)
    def deactivate_watchlist_entry(ticker: str, exchange_code: str) -> None:
        try:
            service.deactivate_watchlist_entry(ticker=ticker, exchange_code=exchange_code)
        except _INFRASTRUCTURE_ERRORS as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="watchlist storage unavailable") from exc

    return app


app = create_app()
