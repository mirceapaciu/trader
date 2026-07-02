from __future__ import annotations

import logging
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

from .backtest_run_request import BacktestRunRequest
from .models import (
    AliasDiscoveryResponse,
    BacklogResponse,
    BacktestCard,
    BacktestCardStatusMetrics,
    BacktestCardTrade,
    BacktestCardsResponse,
    BacktestDelayAggregates,
    BacktestEquityPoint,
    BacktestEquityResponse,
    BacktestEquitySeries,
    BacktestGapMetrics,
    BacktestRegenerationStats,
    BacktestRunDetailResponse,
    BacktestRunProgress,
    BacktestRunSummary,
    BacktestRunsResponse,
    BacktestScalarMetrics,
    BacktestStartRunRequest,
    BacktestStartRunResponse,
    BacktestStrategyMetrics,
    BacktestTrade,
    BacktestTradesResponse,
    DeadLetterResponse,
    DependencyHealth,
    FetchedArticlesResponse,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityStartRunRequest,
    FilterQualityStartRunResponse,
    FilterQualityStatusResponse,
    FilterConfigSimulationStartResponse,
    HealthResponse,
    NewsFilterConfigPayload,
    NewsAnalysesResponse,
    ProvidersResponse,
    ThesisBuilderConsumerHealth,
    ThesisBuilderMetricsResponse,
    ThesisCardListResponse,
    ThesisCardSummary,
    ThesisReprocessRequest,
    ThesisReprocessResponse,
    ThesisReprocessStatusResponse,
    ThroughputGranularity,
    ThroughputResponse,
    WatchlistItemPayload,
    WatchlistItemResponse,
    WatchlistLookupResponse,
    WatchlistLookupSuggestionResponse,
    WatchlistResponse,
    WindowArticlesResponse,
)
from .repository import (
    BacktestCardRow,
    BacktestEquityRow,
    BacktestRunRow,
    BacktestTradeRow,
    BacktesterTablesUnavailable,
)
from .settings import MonitoringUiSettings

logger = logging.getLogger(__name__)

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

    def get_thesis_builder_consumer_health(
        self, *, consumer_group: str
    ) -> ThesisBuilderConsumerHealth: ...

    def fetch_thesis_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        limit: int = 500,
    ) -> list[ThesisCardSummary]: ...

    def get_window_articles(self, *, window_id: int) -> WindowArticlesResponse: ...

    def get_thesis_card_articles(self, *, card_id: str) -> WindowArticlesResponse: ...

    def get_news_analyses(self, *, window_start_at: datetime, window_end_at: datetime, limit: int) -> NewsAnalysesResponse: ...

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

    def list_fetched_articles(
        self,
        *,
        fetched_since: datetime,
        limit: int,
    ) -> FetchedArticlesResponse: ...

    def get_production_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload: ...

    def promote_test_filter_config(self) -> NewsFilterConfigPayload: ...

    def get_running_filter_quality_run(self): ...

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int: ...

    def list_backtest_runs(self, *, window_start_at: datetime) -> list[BacktestRunRow]: ...

    def get_active_backtest_run(self) -> BacktestRunRow | None: ...

    def get_backtest_run(self, *, run_id: str) -> BacktestRunRow | None: ...

    def list_backtest_trades(
        self,
        *,
        run_id: str,
        timing_scenario: str | None = None,
        strategy: str | None = None,
        exit_reason: str | None = None,
        card_status: str | None = None,
        limit: int,
        offset: int,
    ) -> list[BacktestTradeRow]: ...

    def count_backtest_trades(
        self,
        *,
        run_id: str,
        timing_scenario: str | None = None,
        strategy: str | None = None,
        exit_reason: str | None = None,
        card_status: str | None = None,
    ) -> int: ...

    def list_backtest_equity_points(self, *, run_id: str) -> list[BacktestEquityRow]: ...

    def list_backtest_cards(self, *, run_id: str) -> list[BacktestCardRow]: ...


class FilterQualityRunner(Protocol):
    def start_last_24h_run(self, *, accepted_audit_enabled: bool = False) -> str: ...

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str: ...


class BacktestProgressLike(Protocol):
    phase: str
    done: int
    total: int
    current_ticker: str | None
    updated_at: datetime


class BacktestRunner(Protocol):
    def start_run(self, request: BacktestRunRequest) -> str: ...

    def current_progress(self, run_id: str) -> BacktestProgressLike | None: ...


class FilterQualityRunAlreadyActive(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__("filter_quality_run_already_active")
        self.run_id = run_id


class BacktestRunAlreadyActive(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__("backtest_run_already_active")
        self.run_id = run_id


class InvalidBacktestWindow(ValueError):
    pass


class ReprocessRunStatusLike(Protocol):
    run_id: str
    status: str
    days_back: int
    articles_found: int | None
    analyses_created: int | None
    cards_created: int | None
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ThesisReprocessGateway(Protocol):
    def request_reprocess(self, *, days_back: int) -> ReprocessRunStatusLike: ...

    def get_run(self, *, run_id: str) -> ReprocessRunStatusLike | None: ...


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
        reprocess_gateway: ThesisReprocessGateway | None = None,
        backtest_runner: BacktestRunner | None = None,
    ) -> None:
        self._settings = settings
        self._data_source = data_source
        self._filter_quality_runner = filter_quality_runner
        self._watchlist_admin = watchlist_admin
        self._reprocess_gateway = reprocess_gateway
        self._backtest_runner = backtest_runner

    def get_health(self) -> HealthResponse:
        now = _utc_now()
        dependencies = self._data_source.check_dependencies()
        try:
            providers = self._data_source.list_providers()
        except Exception:
            logger.warning("failed to fetch provider list for health check")
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
            logger.warning("provider telemetry unavailable due to infrastructure error")
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
                logger.warning("throughput data unavailable for custom range")
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
            logger.warning("throughput data unavailable for window %s", selected_window)
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
            metrics = self._data_source.get_thesis_builder_metrics(
                window=selected_window,
                evidence_collection_max_minutes=self._settings.thesis_builder_evidence_collection_max_minutes,
            )
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("thesis builder metrics unavailable for window %s", selected_window)
            now = _utc_now()
            metrics = ThesisBuilderMetricsResponse(
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
                dead_letter_count=0,
                recent_dead_letters=[],
                generated_at=now,
            )
        consumer_health = self._thesis_builder_consumer_health()
        if consumer_health is not None:
            metrics = metrics.model_copy(update={"consumer_health": consumer_health})
        return metrics

    def _thesis_builder_consumer_health(self) -> ThesisBuilderConsumerHealth | None:
        try:
            health = self._data_source.get_thesis_builder_consumer_health(
                consumer_group=self._settings.thesis_builder_consumer_group,
            )
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("thesis builder consumer health unavailable")
            return None
        return _evaluate_consumer_stall(
            health,
            threshold_seconds=self._settings.thesis_builder_stall_threshold_seconds,
        )

    def get_thesis_cards(self, *, window: str | None) -> ThesisCardListResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported thesis-builder cards window: {selected_window}")
        now = _utc_now()
        window_start_at = now - _window_duration(selected_window)
        try:
            cards = self._data_source.fetch_thesis_cards(
                window_start_at=window_start_at,
                window_end_at=now,
            )
            return ThesisCardListResponse(
                window=selected_window,
                window_start_at=window_start_at,
                window_end_at=now,
                cards=cards,
                generated_at=now,
            )
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("thesis builder cards unavailable for window %s", selected_window)
            return ThesisCardListResponse(
                available=False,
                message="ThesisBuilder telemetry unavailable.",
                window=selected_window,
                window_start_at=window_start_at,
                window_end_at=now,
                cards=[],
                generated_at=now,
            )

    def get_window_articles(self, *, window_id: int) -> WindowArticlesResponse:
        try:
            return self._data_source.get_window_articles(window_id=window_id)
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("window articles unavailable for window_id %s", window_id)
            return WindowArticlesResponse(
                available=False,
                message="Window articles unavailable.",
                window_id=window_id,
                articles=[],
                generated_at=_utc_now(),
            )

    def get_thesis_card_articles(self, *, card_id: str) -> WindowArticlesResponse:
        try:
            return self._data_source.get_thesis_card_articles(card_id=card_id)
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("thesis card articles unavailable for card_id %s", card_id)
            return WindowArticlesResponse(
                available=False,
                message="Thesis card articles unavailable.",
                card_id=card_id,
                articles=[],
                generated_at=_utc_now(),
            )

    def get_news_analyses(self, *, window: str | None, limit: int) -> NewsAnalysesResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported analyses window: {selected_window}")
        bounded_limit = max(1, min(limit, 500))
        now = _utc_now()
        window_start_at = now - _window_duration(selected_window)
        try:
            return self._data_source.get_news_analyses(
                window_start_at=window_start_at,
                window_end_at=now,
                limit=bounded_limit,
            )
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("news analyses unavailable for window %s", selected_window)
            return NewsAnalysesResponse(
                available=False,
                message="ThesisBuilder telemetry unavailable.",
                generated_at=now,
            )

    def get_backlog(self) -> BacklogResponse:
        try:
            return self._data_source.get_backlog()
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("backlog data unavailable due to infrastructure error")
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
            logger.warning("dead-letter data unavailable due to infrastructure error")
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
            logger.warning("filter-quality data unavailable due to infrastructure error")
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

    def list_fetched_articles(self, *, window: str | None, limit: int) -> FetchedArticlesResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported fetched-articles window: {selected_window}")
        fetched_since = _utc_now() - _window_duration(selected_window)
        try:
            response = self._data_source.list_fetched_articles(fetched_since=fetched_since, limit=limit)
        except _INFRASTRUCTURE_ERRORS:
            logger.warning("fetched articles unavailable for window %s", selected_window)
            return FetchedArticlesResponse(
                available=False,
                message="Fetched articles unavailable.",
                window=selected_window,
                items=[],
                generated_at=_utc_now(),
            )
        return response.model_copy(update={"window": selected_window})

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

    def reprocess_thesis(self, payload: ThesisReprocessRequest) -> ThesisReprocessResponse:
        if self._reprocess_gateway is None:
            raise RuntimeError("reprocess_gateway_unavailable")
        # Enqueues a command on the ThesisBuilder-owned reprocess stream and
        # records an 'accepted' run. ThesisBuilder executes the run in a
        # background thread. ReprocessRunAlreadyActive propagates to the route
        # handler, which maps it to HTTP 409.
        run = self._reprocess_gateway.request_reprocess(days_back=payload.days_back)
        return ThesisReprocessResponse(run_id=run.run_id, status=run.status)

    def get_reprocess_status(self, *, run_id: str) -> ThesisReprocessStatusResponse | None:
        if self._reprocess_gateway is None:
            raise RuntimeError("reprocess_gateway_unavailable")
        run = self._reprocess_gateway.get_run(run_id=run_id)
        if run is None:
            return None
        return ThesisReprocessStatusResponse(
            run_id=run.run_id,
            status=run.status,
            days_back=run.days_back,
            articles_found=run.articles_found,
            analyses_created=run.analyses_created,
            cards_created=run.cards_created,
            error_code=run.error_code,
            requested_at=run.requested_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    def get_backtests(self, *, window: str | None) -> BacktestRunsResponse:
        selected_window = _normalize_throughput_window(window or self._settings.ui_default_time_window)
        if selected_window not in {"15m", "1h", "1d", "7d", "30d"}:
            raise InvalidThroughputWindow(f"Unsupported backtest window: {selected_window}")
        now = _utc_now()
        window_start_at = now - _window_duration(selected_window)
        try:
            runs = self._data_source.list_backtest_runs(window_start_at=window_start_at)
            active = self._data_source.get_active_backtest_run()
        except BacktesterTablesUnavailable:
            return BacktestRunsResponse(
                available=False,
                message="Backtester data unavailable.",
                window=selected_window,
                runs=[],
                active_run=None,
                generated_at=_utc_now(),
            )
        active_summary = None
        if active is not None:
            active_summary = _backtest_run_summary(
                active, progress=self._active_run_progress(active.run_id)
            )
        return BacktestRunsResponse(
            window=selected_window,
            runs=[_backtest_run_summary(row) for row in runs],
            active_run=active_summary,
            generated_at=_utc_now(),
        )

    def _active_run_progress(self, run_id: str) -> BacktestRunProgress | None:
        """Live, in-process progress of the active run, if the coordinator is tracking it."""
        if self._backtest_runner is None:
            return None
        current = self._backtest_runner.current_progress(run_id)
        if current is None:
            return None
        return BacktestRunProgress(
            phase=current.phase,
            done=current.done,
            total=current.total,
            current_ticker=current.current_ticker,
            updated_at=current.updated_at,
        )

    def get_backtest_detail(self, *, run_id: str) -> BacktestRunDetailResponse | None:
        try:
            run = self._data_source.get_backtest_run(run_id=run_id)
        except BacktesterTablesUnavailable:
            return None
        if run is None:
            return None
        per_strategy = _project_per_strategy(run.summary_json)
        card_status = _project_card_status(run.summary_json)
        regeneration = _project_regeneration(run.summary_json)
        gap = (
            BacktestGapMetrics(
                pnl_gap=run.pnl_gap,
                win_rate_gap=run.win_rate_gap,
                trades_flipped_by_delay=run.trades_flipped_by_delay,
            )
            if run.timing_scenario == "both"
            else None
        )
        return BacktestRunDetailResponse(
            run=_backtest_run_summary(run),
            metrics=BacktestScalarMetrics(
                net_pnl=run.net_pnl,
                gross_profit=run.gross_profit,
                gross_loss=run.gross_loss,
                total_commission=run.total_commission,
                total_slippage=run.total_slippage,
                total_return=run.total_return,
                win_rate=run.win_rate,
                avg_win=run.avg_win,
                avg_loss=run.avg_loss,
                profit_factor=run.profit_factor,
                expectancy=run.expectancy,
                max_drawdown=run.max_drawdown,
                max_drawdown_duration_seconds=run.max_drawdown_duration_seconds,
                sharpe_ratio=run.sharpe_ratio,
                exposure_fraction=run.exposure_fraction,
                signal_accuracy=run.signal_accuracy,
                cards_considered=run.cards_considered,
                cards_in_population=run.cards_in_population,
                cards_live_executable=run.cards_live_executable,
                cards_skipped_no_price=run.cards_skipped_no_price,
                trades_opened=run.trades_opened,
                trades_closed=run.trades_closed,
                trades_risk_blocked=run.trades_risk_blocked,
            ),
            per_strategy=per_strategy,
            card_status_breakdown=card_status,
            delays=BacktestDelayAggregates(
                avg_news_fetch_delay_seconds=run.avg_news_fetch_delay_seconds,
                p95_news_fetch_delay_seconds=run.p95_news_fetch_delay_seconds,
                max_news_fetch_delay_seconds=run.max_news_fetch_delay_seconds,
                avg_thesis_build_delay_seconds=run.avg_thesis_build_delay_seconds,
                p95_thesis_build_delay_seconds=run.p95_thesis_build_delay_seconds,
                max_thesis_build_delay_seconds=run.max_thesis_build_delay_seconds,
                avg_total_pipeline_delay_seconds=run.avg_total_pipeline_delay_seconds,
                p95_total_pipeline_delay_seconds=run.p95_total_pipeline_delay_seconds,
                max_total_pipeline_delay_seconds=run.max_total_pipeline_delay_seconds,
            ),
            gap=gap,
            regeneration=regeneration,
            generated_at=_utc_now(),
        )

    def list_backtest_trades(
        self,
        *,
        run_id: str,
        timing_scenario: str | None = None,
        strategy: str | None = None,
        exit_reason: str | None = None,
        card_status: str | None = None,
        limit: int,
        offset: int,
    ) -> BacktestTradesResponse:
        bounded_limit = max(1, min(limit, self._settings.ui_export_max_rows))
        bounded_offset = max(0, offset)
        try:
            total_count = self._data_source.count_backtest_trades(
                run_id=run_id,
                timing_scenario=timing_scenario,
                strategy=strategy,
                exit_reason=exit_reason,
                card_status=card_status,
            )
            trades = self._data_source.list_backtest_trades(
                run_id=run_id,
                timing_scenario=timing_scenario,
                strategy=strategy,
                exit_reason=exit_reason,
                card_status=card_status,
                limit=bounded_limit,
                offset=bounded_offset,
            )
        except BacktesterTablesUnavailable:
            return BacktestTradesResponse(
                available=False,
                message="Backtester data unavailable.",
                run_id=run_id,
                trades=[],
                limit=bounded_limit,
                offset=bounded_offset,
                total_count=0,
                generated_at=_utc_now(),
            )
        return BacktestTradesResponse(
            run_id=run_id,
            trades=[_backtest_trade(row) for row in trades],
            limit=bounded_limit,
            offset=bounded_offset,
            total_count=total_count,
            generated_at=_utc_now(),
        )

    def get_backtest_equity(self, *, run_id: str) -> BacktestEquityResponse:
        try:
            points = self._data_source.list_backtest_equity_points(run_id=run_id)
        except BacktesterTablesUnavailable:
            return BacktestEquityResponse(
                available=False,
                message="Backtester data unavailable.",
                run_id=run_id,
                series=[],
                generated_at=_utc_now(),
            )
        return BacktestEquityResponse(
            run_id=run_id,
            series=_project_equity_series(points),
            generated_at=_utc_now(),
        )

    def list_backtest_cards(self, *, run_id: str) -> BacktestCardsResponse:
        try:
            cards = self._data_source.list_backtest_cards(run_id=run_id)
        except BacktesterTablesUnavailable:
            return BacktestCardsResponse(
                available=False,
                message="Backtester data unavailable.",
                run_id=run_id,
                cards=[],
                generated_at=_utc_now(),
            )
        return BacktestCardsResponse(
            run_id=run_id,
            cards=[
                BacktestCard(
                    thesis_card_id=card.thesis_card_id,
                    ticker=card.ticker,
                    exchange_code=card.exchange_code,
                    direction=card.direction,
                    strategy=card.strategy,
                    time_horizon=card.time_horizon,
                    confidence=card.confidence,
                    decision_state=card.decision_state,
                    card_created_at=card.card_created_at,
                    card_expires_at=card.card_expires_at,
                    trades=[
                        BacktestCardTrade(
                            trade_id=t.trade_id,
                            entry_timing_scenario=t.entry_timing_scenario,
                            entry_at=t.entry_at,
                            entry_price=t.entry_price,
                            exit_at=t.exit_at,
                            exit_price=t.exit_price,
                            net_pnl=t.net_pnl,
                            return_pct=t.return_pct,
                            exit_reason=t.exit_reason,
                            risk_block_rule=t.risk_block_rule,
                        )
                        for t in card.trades
                    ],
                )
                for card in cards
            ],
            generated_at=_utc_now(),
        )

    def start_backtest_run(self, payload: BacktestStartRunRequest) -> BacktestStartRunResponse:
        if self._backtest_runner is None:
            raise RuntimeError("backtest_runner_unavailable")
        window_start_at = _to_utc(payload.window_start_at)
        window_end_at = _to_utc(payload.window_end_at)
        if window_start_at >= window_end_at:
            raise InvalidBacktestWindow("Backtest window must have window_start_at earlier than window_end_at.")
        request = BacktestRunRequest(
            window_start_at=window_start_at,
            window_end_at=window_end_at,
            mode=payload.mode,
            timing_scenario=payload.timing_scenario,
            card_population=payload.card_population,
            strategies=list(payload.strategies) if payload.strategies else None,
            initial_capital=payload.initial_capital,
            run_note=payload.run_note,
            llm_model=payload.llm_model,
            required_evidence_count=payload.required_evidence_count,
            evidence_collection_max_minutes=payload.evidence_collection_max_minutes,
        )
        try:
            run_id = self._backtest_runner.start_run(request)
        except ValueError as exc:
            raise InvalidBacktestWindow(str(exc)) from exc
        return BacktestStartRunResponse(run_id=run_id, status="running")

    def _require_watchlist_admin(self) -> SharedInstrumentLookupAdminService:
        if self._watchlist_admin is None:
            raise RuntimeError("watchlist_admin_unavailable")
        return self._watchlist_admin


def _dependency_healthy(dependencies: list[DependencyHealth], kind: str) -> bool:
    return any(dependency.kind == kind and dependency.state == "healthy" for dependency in dependencies)


def _evaluate_consumer_stall(
    health: ThesisBuilderConsumerHealth, *, threshold_seconds: int
) -> ThesisBuilderConsumerHealth:
    """Decide whether the ThesisBuilder consumer is stalled from raw signals.

    A stall is only flagged when there is a backlog to drain — otherwise a quiet
    period with nothing to process would read as a false alarm. Given a backlog,
    the consumer is stalled if no consumer has read the queue recently or no
    analyses have come out within the threshold.
    """
    if not health.available or not health.group_present:
        return health

    backlog_size = (health.consumer_lag or 0) + (health.pending_count or 0)
    if backlog_size <= 0:
        return health.model_copy(update={"stalled": False, "stall_reasons": []})

    reasons: list[str] = []

    if (health.consumer_count or 0) == 0:
        reasons.append("No ThesisBuilder consumer is connected to the queue.")
    elif (
        health.min_consumer_idle_seconds is not None
        and health.min_consumer_idle_seconds > threshold_seconds
    ):
        reasons.append(
            f"No consumer has read the queue in {_format_duration(health.min_consumer_idle_seconds)} "
            f"(threshold {_format_duration(threshold_seconds)})."
        )

    if health.last_analysis_age_seconds is None:
        reasons.append(
            f"No analyses have ever been produced while {backlog_size} message(s) await processing."
        )
    elif health.last_analysis_age_seconds > threshold_seconds:
        reasons.append(
            f"No analyses produced in {_format_duration(health.last_analysis_age_seconds)} "
            f"while {backlog_size} message(s) await processing."
        )

    return health.model_copy(update={"stalled": bool(reasons), "stall_reasons": reasons})


def _format_duration(seconds: float) -> str:
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


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


_CARD_STATUS_BUCKETS = (
    "approved",
    "rejected",
    "card_was_live_expired",
    "card_unexpired_at_entry",
)


def _backtest_run_summary(
    row: BacktestRunRow, *, progress: BacktestRunProgress | None = None
) -> BacktestRunSummary:
    return BacktestRunSummary(
        progress=progress,
        run_id=row.run_id,
        status=row.status,
        window_start_at=row.window_start_at,
        window_end_at=row.window_end_at,
        mode=row.mode,
        timing_scenario=row.timing_scenario,
        card_population=row.card_population,
        strategies_requested=row.strategies_requested,
        initial_capital=row.initial_capital,
        llm_model=row.llm_model,
        net_pnl=row.net_pnl,
        total_return=row.total_return,
        win_rate=row.win_rate,
        profit_factor=row.profit_factor,
        max_drawdown=row.max_drawdown,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )


def _backtest_trade(row: BacktestTradeRow) -> BacktestTrade:
    return BacktestTrade(
        trade_id=row.trade_id,
        ticker=row.ticker,
        exchange_code=row.exchange_code,
        strategy=row.strategy,
        direction=row.direction,
        entry_timing_scenario=row.entry_timing_scenario,
        entry_at=row.entry_at,
        entry_price=row.entry_price,
        exit_at=row.exit_at,
        exit_price=row.exit_price,
        net_pnl=row.net_pnl,
        return_pct=row.return_pct,
        exit_reason=row.exit_reason,
        risk_block_rule=row.risk_block_rule,
        news_fetch_delay_seconds=row.news_fetch_delay_seconds,
        thesis_build_delay_seconds=row.thesis_build_delay_seconds,
        total_pipeline_delay_seconds=row.total_pipeline_delay_seconds,
        card_decision_state=row.card_decision_state,
        card_was_live_expired=row.card_was_live_expired,
    )


def _project_per_strategy(summary_json: dict) -> list[BacktestStrategyMetrics]:
    by_strategy = summary_json.get("by_strategy")
    if not isinstance(by_strategy, dict):
        return []
    metrics: list[BacktestStrategyMetrics] = []
    for strategy in sorted(by_strategy):
        agg = by_strategy.get(strategy)
        if not isinstance(agg, dict):
            continue
        metrics.append(BacktestStrategyMetrics(strategy=strategy, **_agg_fields(agg)))
    return metrics


def _project_card_status(summary_json: dict) -> list[BacktestCardStatusMetrics]:
    by_card_status = summary_json.get("by_card_status")
    if not isinstance(by_card_status, dict):
        return []
    ordered = [bucket for bucket in _CARD_STATUS_BUCKETS if bucket in by_card_status]
    ordered.extend(bucket for bucket in by_card_status if bucket not in _CARD_STATUS_BUCKETS)
    metrics: list[BacktestCardStatusMetrics] = []
    for bucket in ordered:
        agg = by_card_status.get(bucket)
        if not isinstance(agg, dict):
            continue
        metrics.append(BacktestCardStatusMetrics(bucket=bucket, **_agg_fields(agg)))
    return metrics


def _project_regeneration(summary_json: dict) -> BacktestRegenerationStats | None:
    regen = summary_json.get("regeneration")
    if not isinstance(regen, dict):
        return None
    return BacktestRegenerationStats(
        articles_found=regen.get("articles_found"),
        articles_relevant=regen.get("articles_relevant"),
        articles_analyzed=regen.get("articles_analyzed"),
        analyses_created=regen.get("analyses_created"),
        cards_created=regen.get("cards_created"),
        evidence_windows_created=regen.get("evidence_windows_created"),
        budget_exhausted=regen.get("budget_exhausted"),
    )


def _agg_fields(agg: dict) -> dict:
    return {
        "trades_opened": agg.get("trades_opened"),
        "trades_closed": agg.get("trades_closed"),
        "trades_risk_blocked": agg.get("trades_risk_blocked"),
        "net_pnl": agg.get("net_pnl"),
        "gross_profit": agg.get("gross_profit"),
        "gross_loss": agg.get("gross_loss"),
        "win_rate": agg.get("win_rate"),
        "avg_win": agg.get("avg_win"),
        "avg_loss": agg.get("avg_loss"),
        "profit_factor": agg.get("profit_factor"),
        "expectancy": agg.get("expectancy"),
    }


def _project_equity_series(points: list[BacktestEquityRow]) -> list[BacktestEquitySeries]:
    grouped: dict[str, list[BacktestEquityPoint]] = {}
    for point in points:
        grouped.setdefault(point.timing_scenario, []).append(
            BacktestEquityPoint(
                as_of=point.as_of,
                equity=point.equity,
                open_positions=point.open_positions,
            )
        )
    return [
        BacktestEquitySeries(timing_scenario=scenario, points=grouped[scenario])
        for scenario in sorted(grouped)
    ]


def _lookup_suggestion_response(item: InstrumentLookupSuggestion) -> WatchlistLookupSuggestionResponse:
    return WatchlistLookupSuggestionResponse(
        ticker=item.ticker,
        exchange_code=item.exchange_code,
        display_name=item.display_name,
        aliases=list(item.aliases),
        provider=item.provider,
    )
