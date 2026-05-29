from __future__ import annotations

import re

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .models import BacklogResponse, DeadLetterResponse, HealthResponse, ProvidersResponse, ThroughputResponse
from .repository import PostgresRedisMonitoringDataSource
from .service import MonitoringService
from .settings import MonitoringUiSettings


def _local_dev_origin_regex() -> str:
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def create_app(settings: MonitoringUiSettings | None = None) -> FastAPI:
    resolved_settings = settings or MonitoringUiSettings.from_env()
    data_source = PostgresRedisMonitoringDataSource(
        dsn=resolved_settings.postgres_dsn,
        news_schema=resolved_settings.newsfetcher_db_schema,
        queue_url=resolved_settings.queue_url,
        news_raw_queue=resolved_settings.news_raw_queue,
        query_timeout_seconds=resolved_settings.ui_query_timeout_seconds,
    )
    service = MonitoringService(settings=resolved_settings, data_source=data_source)

    app = FastAPI(title="Trader Monitoring UI API")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_local_dev_origin_regex(),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    def get_health() -> HealthResponse:
        return service.get_health()

    @app.get("/api/providers", response_model=ProvidersResponse)
    def list_providers() -> ProvidersResponse:
        return service.list_providers()

    @app.get("/api/metrics/throughput", response_model=ThroughputResponse)
    def get_throughput(window: str | None = Query(default=None)) -> ThroughputResponse:
        return service.get_throughput(window=window)

    @app.get("/api/backlog", response_model=BacklogResponse)
    def get_backlog() -> BacklogResponse:
        return service.get_backlog()

    @app.get("/api/dead-letter", response_model=DeadLetterResponse)
    def list_dead_letters(
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> DeadLetterResponse:
        return service.list_dead_letters(limit=limit, offset=offset)

    @app.post("/api/actions/refresh")
    def refresh() -> dict[str, str]:
        return {"status": "accepted"}

    @app.post("/api/actions/alert-test")
    def alert_test() -> dict[str, str]:
        return {"status": "accepted"}

    return app


app = create_app()
