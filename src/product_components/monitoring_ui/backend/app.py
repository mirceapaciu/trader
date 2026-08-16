from __future__ import annotations

import logging
import threading
from pathlib import Path

from datetime import datetime
from typing import Callable, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
import psycopg
import redis

from fastapi.responses import FileResponse

from .backtest_runner import BacktestRunCoordinator
from .filter_quality_runner import FilterQualityRunCoordinator
from .models import (
    AliasDiscoveryResponse,
    BacklogResponse,
    BacktestCardsResponse,
    BacktestEquityResponse,
    BacktestRunDetailResponse,
    BacktestRunsResponse,
    BacktestStartRunRequest,
    BacktestStartRunResponse,
    BacktestTradesResponse,
    DeadLetterResponse,
    FetchedArticlesResponse,
    FilterConfigSimulationStartResponse,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunRequest,
    FilterQualityStartRunResponse,
    FilterQualityStatusResponse,
    HealthResponse,
    NewsAnalysesResponse,
    NewsFetcherReprocessRejectedRequest,
    NewsFetcherReprocessRejectedResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ThesisBuilderConfigResponse,
    ThesisBuilderMetricsResponse,
    ThesisBuilderTaxonomyGapsResponse,
    ThesisBuilderTaxonomyValuesResponse,
    ThesisBuilderTaxonomyDecisionRequest,
    ThesisBuilderTaxonomyDecisionResponse,
    ThesisBuilderThroughputResponse,
    ThesisCardListResponse,
    ThesisReprocessRequest,
    ThesisReprocessResponse,
    ThesisReprocessStatusResponse,
    ThroughputResponse,
    WatchlistItemPayload,
    WatchlistItemResponse,
    WatchlistLookupResponse,
    WatchlistResponse,
    WindowArticlesResponse,
)
from .repository import PostgresRedisMonitoringDataSource
from src.product_components.backtester.repository import bootstrap_backtester_schema
from src.product_components.news_fetcher.reprocess import NewsFetcherRejectedArticleReprocessor
from src.product_components.news_fetcher.settings import NewsFetcherSettings
from src.product_components.news_fetcher.storage_adapter import PostgresNewsStorageAdapter
from .service import (
    BacktestRunAlreadyActive,
    FilterQualityRunAlreadyActive,
    InvalidBacktestWindow,
    InvalidThroughputWindow,
    MonitoringService,
)
from .settings import MonitoringUiSettings
from .admin_auth import AdminSessionStore, require_admin_session, require_csrf, session_cookie_name
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
)
from src.product_components.thesis_builder.repository import PostgresThesisBuilderRepository
from src.product_components.thesis_builder.reprocess_gateway import (
    RedisReprocessCommandPublisher,
    ReprocessRunAlreadyActive,
    ThesisReprocessGateway,
)
from src.product_components.thesis_builder.taxonomy_gateway import (
    RedisTaxonomyCommandPublisher,
    ThesisTaxonomyDecisionGateway,
)
from src.product_components.thesis_builder.settings import ThesisBuilderSettings
from src.product_components.shared.instrument_lookup import (
    AlphaVantageInstrumentLookupProvider,
    DuplicateActiveWatchlistEntry,
    MassiveInstrumentLookupProvider,
    OpenFigiInstrumentLookupProvider,
    SharedInstrumentLookupAdminService,
)

logger = logging.getLogger(__name__)

_INFRASTRUCTURE_ERRORS = (psycopg.Error, redis.RedisError, TimeoutError)
_T = TypeVar("_T")


def _local_dev_origin_regex() -> str:
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _frontend_dist_dir() -> Path:
    return _repo_root() / "src" / "product_components" / "monitoring_ui" / "frontend" / "dist"


def _admin_auth_available(*, settings: MonitoringUiSettings) -> None:
    if not settings.taxonomy_decisions_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="taxonomy_decisions_disabled",
        )
    if not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin_auth_unavailable",
        )


def _request_origin(settings: MonitoringUiSettings) -> str:
    if settings.admin_allowed_origin:
        return settings.admin_allowed_origin
    return settings.ui_api_base_url.removesuffix("/api").rstrip("/")


def _mount_frontend(app: FastAPI) -> None:
    dist_dir = _frontend_dist_dir()
    index_path = dist_dir / "index.html"
    if not index_path.is_file():
        logger.info("monitoring UI frontend build not found at %s; serving API only", dist_dir)
        return

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_asset_or_index(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        candidate = (dist_dir / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist_dir):
            return FileResponse(candidate)
        return FileResponse(index_path)


def _bootstrap_schemas_in_background(
    *,
    data_source: PostgresRedisMonitoringDataSource,
    resolved_settings: MonitoringUiSettings,
) -> None:
    def _bootstrap() -> None:
        try:
            data_source.bootstrap_shared_schema(repo_root=_repo_root())
            data_source.bootstrap_news_schema(repo_root=_repo_root())
            data_source.bootstrap_thesis_builder_schema(repo_root=_repo_root())
            bootstrap_backtester_schema(
                dsn=resolved_settings.postgres_dsn,
                repo_root=_repo_root(),
            )
        except Exception:
            logger.exception("schema bootstrap failed — starting in degraded mode")

    threading.Thread(target=_bootstrap, name="monitoring-ui-schema-bootstrap", daemon=True).start()


def _run_with_infrastructure_mapping(operation: Callable[[], _T], *, detail: str) -> _T:
    try:
        return operation()
    except _INFRASTRUCTURE_ERRORS as exc:
        logger.warning("infrastructure error mapped to 503: %s — %s", detail, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc


def create_app(
    settings: MonitoringUiSettings | None = None,
    *,
    bootstrap_schemas: bool | None = None,
) -> FastAPI:
    resolved_settings = settings or MonitoringUiSettings.from_env()
    resolved_settings.validate_admin_auth()
    admin_sessions = AdminSessionStore(
        password=resolved_settings.admin_password,
        session_ttl_seconds=resolved_settings.admin_session_ttl_seconds,
        login_window_seconds=resolved_settings.admin_login_window_seconds,
        login_max_attempts=resolved_settings.admin_login_max_attempts,
    )
    should_bootstrap_schemas = bootstrap_schemas if bootstrap_schemas is not None else settings is None
    thesis_builder_settings = ThesisBuilderSettings.from_env()
    data_source = PostgresRedisMonitoringDataSource(
        dsn=resolved_settings.postgres_dsn,
        news_schema=resolved_settings.newsfetcher_db_schema,
        filter_quality_schema=resolved_settings.filter_quality_db_schema,
        thesis_builder_schema=thesis_builder_settings.thesis_builder_db_schema,
        queue_url=resolved_settings.queue_url,
        news_raw_queue=resolved_settings.news_raw_queue,
        failed_messages_dlq=resolved_settings.failed_messages_dlq,
        query_timeout_seconds=resolved_settings.ui_query_timeout_seconds,
        backtester_schema=resolved_settings.backtester_db_schema,
    )
    news_fetcher_settings = NewsFetcherSettings.from_env()
    instrument_registry = PostgresSharedInstrumentRegistry(
        dsn=resolved_settings.postgres_dsn,
        shared_schema=resolved_settings.shared_db_schema,
        watchlist_table=resolved_settings.watchlist_table,
    )
    watchlist_admin = SharedInstrumentLookupAdminService(
        registry=instrument_registry,
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
    reprocess_gateway = ThesisReprocessGateway(
        repository=PostgresThesisBuilderRepository(
            dsn=resolved_settings.postgres_dsn,
            thesis_schema=thesis_builder_settings.thesis_builder_db_schema,
        ),
        command_publisher=RedisReprocessCommandPublisher(
            queue_url=resolved_settings.queue_url,
            command_stream=resolved_settings.reprocess_command_queue,
        ),
    )
    taxonomy_decision_gateway = ThesisTaxonomyDecisionGateway(
        repository=PostgresThesisBuilderRepository(
            dsn=resolved_settings.postgres_dsn,
            thesis_schema=thesis_builder_settings.thesis_builder_db_schema,
        ),
        command_publisher=RedisTaxonomyCommandPublisher(
            queue_url=resolved_settings.queue_url,
            command_stream=resolved_settings.taxonomy_command_queue,
        ),
    )
    news_fetcher_reprocessor = NewsFetcherRejectedArticleReprocessor(
        settings=news_fetcher_settings,
        storage=PostgresNewsStorageAdapter(
            dsn=resolved_settings.postgres_dsn,
            news_schema=resolved_settings.newsfetcher_db_schema,
            instrument_registry=instrument_registry,
        ),
    )
    service = MonitoringService(
        settings=resolved_settings,
        data_source=data_source,
        thesis_builder_settings=thesis_builder_settings,
        filter_quality_runner=FilterQualityRunCoordinator(),
        watchlist_admin=watchlist_admin,
        reprocess_gateway=reprocess_gateway,
        taxonomy_decision_gateway=taxonomy_decision_gateway,
        news_fetcher_reprocessor=news_fetcher_reprocessor,
        backtest_runner=BacktestRunCoordinator(),
    )

    app = FastAPI(title="Trader Monitoring UI API")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_local_dev_origin_regex(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    if should_bootstrap_schemas:
        _bootstrap_schemas_in_background(data_source=data_source, resolved_settings=resolved_settings)

    @app.post("/api/admin/login")
    def admin_login(payload: dict[str, str], request: Request, response: Response) -> dict[str, str | bool]:
        _admin_auth_available(settings=resolved_settings)
        session = admin_sessions.login(
            username=payload.get("username", ""), password=payload.get("password", ""),
            source=request.client.host if request.client else "unknown",
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        secure = _request_origin(resolved_settings).startswith("https://")
        response.set_cookie(session_cookie_name(), session.session_id, httponly=True, samesite="strict", secure=secure, max_age=resolved_settings.admin_session_ttl_seconds, path="/")
        return {"authenticated": True, "actor": "admin", "csrf_token": session.csrf_token}

    @app.get("/api/admin/session")
    def admin_session(request: Request) -> dict[str, str | bool | None]:
        session = admin_sessions.get(request.cookies.get(session_cookie_name()))
        return {"authenticated": session is not None, "actor": "admin" if session else None, "csrf_token": session.csrf_token if session else None}

    @app.post("/api/admin/logout", status_code=status.HTTP_204_NO_CONTENT)
    def admin_logout(request: Request, response: Response) -> Response:
        session = require_admin_session(request=request, store=admin_sessions)
        require_csrf(request=request, session=session, allowed_origin=_request_origin(resolved_settings))
        admin_sessions.logout(session.session_id)
        response.delete_cookie(session_cookie_name(), path="/")
        return response

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

    @app.get("/api/thesis-builder/throughput", response_model=ThesisBuilderThroughputResponse)
    def get_thesis_builder_throughput(window: str | None = Query(default=None)) -> ThesisBuilderThroughputResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_thesis_builder_throughput(window=window),
                detail="thesis-builder throughput unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/thesis-builder/config", response_model=ThesisBuilderConfigResponse)
    def get_thesis_builder_config() -> ThesisBuilderConfigResponse:
        return service.get_thesis_builder_config()

    @app.get("/api/thesis-builder/cards", response_model=ThesisCardListResponse)
    def get_thesis_cards(window: str | None = Query(default=None)) -> ThesisCardListResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_thesis_cards(window=window),
                detail="thesis-builder cards unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get(
        "/api/thesis-builder/windows/{window_id}/articles",
        response_model=WindowArticlesResponse,
    )
    def get_window_articles(window_id: int) -> WindowArticlesResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.get_window_articles(window_id=window_id),
            detail="window articles unavailable",
        )

    @app.get("/api/thesis-builder/analyses", response_model=NewsAnalysesResponse)
    def get_news_analyses(
        window: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> NewsAnalysesResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_news_analyses(window=window, limit=limit),
                detail="thesis-builder analyses unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get(
        "/api/thesis-builder/cards/{card_id}/articles",
        response_model=WindowArticlesResponse,
    )
    def get_thesis_card_articles(card_id: str) -> WindowArticlesResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.get_thesis_card_articles(card_id=card_id),
            detail="thesis card articles unavailable",
        )

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

    @app.get("/api/news-fetcher/fetched-articles", response_model=FetchedArticlesResponse)
    def list_fetched_articles(
        window: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> FetchedArticlesResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.list_fetched_articles(window=window, limit=limit),
                detail="fetched articles unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/thesis-builder/taxonomy-gaps", response_model=ThesisBuilderTaxonomyGapsResponse)
    def get_thesis_builder_taxonomy_gaps() -> ThesisBuilderTaxonomyGapsResponse:
        return _run_with_infrastructure_mapping(
            service.get_thesis_builder_taxonomy_gaps,
            detail="thesis-builder taxonomy gaps unavailable",
        )

    @app.get(
        "/api/thesis-builder/taxonomy-values",
        response_model=ThesisBuilderTaxonomyValuesResponse,
    )
    def get_thesis_builder_taxonomy_values(
        dimension: str = Query(min_length=1, max_length=80),
        family_scope: str | None = Query(default=None, max_length=80),
    ) -> ThesisBuilderTaxonomyValuesResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.get_thesis_builder_taxonomy_values(
                dimension=dimension,
                family_scope=family_scope,
            ),
            detail="thesis-builder taxonomy values unavailable",
        )

    @app.post(
        "/api/thesis-builder/taxonomy-decisions",
        response_model=ThesisBuilderTaxonomyDecisionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def decide_thesis_builder_taxonomy_gap(
        payload: ThesisBuilderTaxonomyDecisionRequest,
        request: Request,
    ) -> ThesisBuilderTaxonomyDecisionResponse:
        _admin_auth_available(settings=resolved_settings)
        admin_session = require_admin_session(request=request, store=admin_sessions)
        require_csrf(request=request, session=admin_session, allowed_origin=_request_origin(resolved_settings))
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.decide_taxonomy_gap(payload, actor="admin"),
                detail="thesis-builder taxonomy decision unavailable",
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get(
        "/api/thesis-builder/taxonomy-decisions/{command_id}",
        response_model=ThesisBuilderTaxonomyDecisionResponse,
    )
    def get_thesis_builder_taxonomy_decision(
        command_id: str,
        request: Request,
    ) -> ThesisBuilderTaxonomyDecisionResponse:
        _admin_auth_available(settings=resolved_settings)
        require_admin_session(request=request, store=admin_sessions)
        result = _run_with_infrastructure_mapping(
            lambda: service.get_taxonomy_decision_status(command_id=command_id),
            detail="thesis-builder taxonomy decision unavailable",
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="taxonomy_command_not_found",
            )
        return result

    @app.post(
        "/api/news-fetcher/reprocess-rejected",
        response_model=NewsFetcherReprocessRejectedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reprocess_news_fetcher_rejected(
        payload: NewsFetcherReprocessRejectedRequest,
    ) -> NewsFetcherReprocessRejectedResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.reprocess_news_fetcher_rejected(payload),
                detail="news-fetcher reprocessing unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

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

    @app.get("/api/backtests", response_model=BacktestRunsResponse)
    def get_backtests(window: str | None = Query(default=None)) -> BacktestRunsResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.get_backtests(window=window),
                detail="backtest data unavailable",
            )
        except InvalidThroughputWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.get("/api/backtests/{run_id}", response_model=BacktestRunDetailResponse)
    def get_backtest_detail(run_id: str) -> BacktestRunDetailResponse:
        result = _run_with_infrastructure_mapping(
            lambda: service.get_backtest_detail(run_id=run_id),
            detail="backtest data unavailable",
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest run not found")
        return result

    @app.get("/api/backtests/{run_id}/trades", response_model=BacktestTradesResponse)
    def list_backtest_trades(
        run_id: str,
        timing_scenario: str | None = Query(default=None),
        strategy: str | None = Query(default=None),
        exit_reason: str | None = Query(default=None),
        card_status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> BacktestTradesResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.list_backtest_trades(
                run_id=run_id,
                timing_scenario=timing_scenario,
                strategy=strategy,
                exit_reason=exit_reason,
                card_status=card_status,
                limit=limit,
                offset=offset,
            ),
            detail="backtest trades unavailable",
        )

    @app.get("/api/backtests/{run_id}/equity", response_model=BacktestEquityResponse)
    def get_backtest_equity(run_id: str) -> BacktestEquityResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.get_backtest_equity(run_id=run_id),
            detail="backtest equity unavailable",
        )

    @app.get("/api/backtests/{run_id}/cards", response_model=BacktestCardsResponse)
    def list_backtest_cards(run_id: str) -> BacktestCardsResponse:
        return _run_with_infrastructure_mapping(
            lambda: service.list_backtest_cards(run_id=run_id),
            detail="backtest cards unavailable",
        )

    @app.post(
        "/api/backtests",
        response_model=BacktestStartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_backtest_run(payload: BacktestStartRunRequest) -> BacktestStartRunResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.start_backtest_run(payload),
                detail="backtest runner unavailable",
            )
        except BacktestRunAlreadyActive as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"run_id": exc.run_id, "status": "running", "message": "already running"},
            ) from exc
        except InvalidBacktestWindow as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post(
        "/api/thesis-builder/reprocess",
        response_model=ThesisReprocessResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reprocess_thesis(payload: ThesisReprocessRequest) -> ThesisReprocessResponse:
        try:
            return _run_with_infrastructure_mapping(
                lambda: service.reprocess_thesis(payload),
                detail="thesis reprocessing unavailable",
            )
        except ReprocessRunAlreadyActive as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"run_id": exc.run_id, "status": "running", "message": "already running"},
            ) from exc

    @app.get(
        "/api/thesis-builder/reprocess/{run_id}",
        response_model=ThesisReprocessStatusResponse,
    )
    def get_reprocess_status(run_id: str) -> ThesisReprocessStatusResponse:
        result = _run_with_infrastructure_mapping(
            lambda: service.get_reprocess_status(run_id=run_id),
            detail="thesis reprocessing unavailable",
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="reprocess run not found",
            )
        return result

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
            logger.warning("watchlist storage unavailable: %s", exc)
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
            logger.warning("watchlist storage unavailable: %s", exc)
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
            logger.warning("watchlist storage unavailable: %s", exc)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="watchlist storage unavailable") from exc

    _mount_frontend(app)
    return app


app = create_app()
