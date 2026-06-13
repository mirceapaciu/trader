from __future__ import annotations

import re
from pathlib import Path

from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from .filter_quality_runner import FilterQualityRunCoordinator
from .models import (
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
    ThroughputResponse,
)
from .repository import PostgresRedisMonitoringDataSource
from .service import FilterQualityRunAlreadyActive, InvalidThroughputWindow, MonitoringService
from .settings import MonitoringUiSettings


def _local_dev_origin_regex() -> str:
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def create_app(settings: MonitoringUiSettings | None = None) -> FastAPI:
    resolved_settings = settings or MonitoringUiSettings.from_env()
    data_source = PostgresRedisMonitoringDataSource(
        dsn=resolved_settings.postgres_dsn,
        news_schema=resolved_settings.newsfetcher_db_schema,
        filter_quality_schema=resolved_settings.filter_quality_db_schema,
        queue_url=resolved_settings.queue_url,
        news_raw_queue=resolved_settings.news_raw_queue,
        query_timeout_seconds=resolved_settings.ui_query_timeout_seconds,
    )
    try:
        data_source.bootstrap_news_schema(repo_root=_repo_root())
    except Exception:
        # The API still starts in degraded mode; dependency health reports database issues separately.
        pass
    service = MonitoringService(
        settings=resolved_settings,
        data_source=data_source,
        filter_quality_runner=FilterQualityRunCoordinator(),
    )

    app = FastAPI(title="Trader Monitoring UI API")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_local_dev_origin_regex(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    def get_health() -> HealthResponse:
        return service.get_health()

    @app.get("/api/providers", response_model=ProvidersResponse)
    def list_providers() -> ProvidersResponse:
        return service.list_providers()

    @app.get("/api/metrics/throughput", response_model=ThroughputResponse)
    def get_throughput(
        window: str | None = Query(default=None),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
    ) -> ThroughputResponse:
        try:
            return service.get_throughput(window=window, start_at=start_at, end_at=end_at)
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/backlog", response_model=BacklogResponse)
    def get_backlog() -> BacklogResponse:
        return service.get_backlog()

    @app.get("/api/dead-letter", response_model=DeadLetterResponse)
    def list_dead_letters(
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> DeadLetterResponse:
        return service.list_dead_letters(limit=limit, offset=offset)

    @app.get("/api/filter-quality", response_model=FilterQualityStatusResponse)
    def get_filter_quality() -> FilterQualityStatusResponse:
        return service.get_filter_quality_status()

    @app.get(
        "/api/filter-quality/runs/{run_id}/incorrectly-rejected",
        response_model=FilterQualityIncorrectlyRejectedResponse,
    )
    def list_filter_quality_incorrectly_rejected(run_id: str) -> FilterQualityIncorrectlyRejectedResponse:
        return service.list_filter_quality_incorrectly_rejected(run_id=run_id)

    @app.get(
        "/api/filter-quality/runs/{run_id}/incorrectly-accepted",
        response_model=FilterQualityIncorrectlyAcceptedResponse,
    )
    def list_filter_quality_incorrectly_accepted(run_id: str) -> FilterQualityIncorrectlyAcceptedResponse:
        return service.list_filter_quality_incorrectly_accepted(run_id=run_id)

    @app.get("/api/filter-configs/production", response_model=NewsFilterConfigPayload)
    def get_production_filter_config() -> NewsFilterConfigPayload:
        return service.get_production_filter_config()

    @app.get("/api/filter-configs/test", response_model=NewsFilterConfigPayload)
    def get_test_filter_config() -> NewsFilterConfigPayload:
        return service.get_test_filter_config()

    @app.put("/api/filter-configs/test", response_model=NewsFilterConfigPayload)
    def save_test_filter_config(payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        return service.save_test_filter_config(payload)

    @app.post(
        "/api/filter-configs/test/simulations",
        response_model=FilterConfigSimulationStartResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_test_filter_simulation() -> FilterConfigSimulationStartResponse:
        try:
            return service.start_test_filter_simulation()
        except FilterQualityRunAlreadyActive as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"run_id": exc.run_id, "status": "running", "message": "already running"},
            ) from exc

    @app.post("/api/filter-configs/test/promote", response_model=NewsFilterConfigPayload)
    def promote_test_filter_config() -> NewsFilterConfigPayload:
        return service.promote_test_filter_config()

    @app.post(
        "/api/filter-quality/runs",
        response_model=FilterQualityStartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_filter_quality_run(payload: FilterQualityStartRunRequest | None = None) -> FilterQualityStartRunResponse:
        try:
            return service.start_filter_quality_run(payload)
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

    return app


app = create_app()
