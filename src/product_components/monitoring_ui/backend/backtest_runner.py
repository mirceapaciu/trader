from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from src.product_components.backtester.clients import MarketDataBarsProvider
from src.product_components.backtester.models import (
    BacktestMode,
    BacktestRunParams,
    CardPopulation,
    TimingScenario,
)
from src.product_components.backtester.repository import BacktesterRepository
from src.product_components.backtester.service import BacktesterService, new_run_id
from src.product_components.backtester.settings import BacktesterSettings
from src.product_components.market_data.providers import build_provider_clients
from src.product_components.market_data.service import MarketDataService
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter
from src.product_components.shared.adapters import (
    PostgresSharedApiUsageWriter,
    PostgresSharedInstrumentRegistry,
)
from src.product_components.thesis_builder.export import ThesisCardHistoryExporter

from .backtest_run_request import BacktestRunRequest
from .service import BacktestRunAlreadyActive

LOGGER = logging.getLogger(__name__)


class BacktestRunCoordinator:
    """Runs a backtester in-process, one at a time, mirroring backtester/main.py wiring."""

    def __init__(
        self,
        *,
        settings_factory: Callable[[], BacktesterSettings] = BacktesterSettings.from_env,
        market_data_settings_factory: Callable[[], MarketDataSettings] = MarketDataSettings.from_env,
        run_id_factory: Callable[[], str] = new_run_id,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings_factory = settings_factory
        self._market_data_settings_factory = market_data_settings_factory
        self._run_id_factory = run_id_factory
        self._now_factory = now_factory or _utc_now
        self._lock = threading.Lock()
        self._active_run_id: str | None = None

    def start_run(self, request: BacktestRunRequest) -> str:
        params = self._build_params(request)
        with self._lock:
            if self._active_run_id is not None:
                raise BacktestRunAlreadyActive(self._active_run_id)
            self._active_run_id = params.run_id

        thread = threading.Thread(
            target=self._run_in_background,
            args=(params,),
            name=f"backtester-{params.run_id}",
            daemon=True,
        )
        thread.start()
        return params.run_id

    def _build_params(self, request: BacktestRunRequest) -> BacktestRunParams:
        window_start_at = _to_utc(request.window_start_at)
        window_end_at = _to_utc(request.window_end_at)
        if window_start_at >= window_end_at:
            raise ValueError("invalid_window")
        settings = self._settings_factory()
        initial_capital = (
            request.initial_capital
            if request.initial_capital is not None
            else settings.initial_capital_usd
        )
        return BacktestRunParams(
            run_id=self._run_id_factory(),
            window_start_at=window_start_at,
            window_end_at=window_end_at,
            mode=BacktestMode(request.mode),
            timing_scenario=TimingScenario(request.timing_scenario),
            card_population=CardPopulation(request.card_population),
            strategies=list(request.strategies) if request.strategies else None,
            initial_capital=initial_capital,
            ideal_fetch_delay_seconds=settings.ideal_fetch_delay_seconds,
            ideal_thesis_delay_seconds=settings.ideal_thesis_delay_seconds,
            run_note=request.run_note,
        )

    def _run_in_background(self, params: BacktestRunParams) -> None:
        try:
            settings = self._settings_factory()
            market_data_settings = self._market_data_settings_factory()
            instrument_registry = PostgresSharedInstrumentRegistry(
                dsn=settings.postgres_dsn,
                shared_schema=settings.shared_db_schema,
                watchlist_table=market_data_settings.watchlist_table,
            )
            market_data_service = MarketDataService(
                storage=PostgresMarketDataStorageAdapter(
                    dsn=market_data_settings.postgres_dsn,
                    market_data_schema=market_data_settings.market_data_db_schema,
                    instrument_registry=instrument_registry,
                    api_usage_writer=PostgresSharedApiUsageWriter(
                        dsn=market_data_settings.postgres_dsn,
                        shared_schema=market_data_settings.shared_db_schema,
                    ),
                ),
                provider_clients=build_provider_clients(market_data_settings),
                quote_max_age_seconds=market_data_settings.quote_max_age_seconds,
                daily_bar_lookback_days=market_data_settings.daily_bar_lookback_days,
                historical_bars_provider=market_data_settings.historical_bars_provider,
                max_requests_per_minute=market_data_settings.polygon_max_requests_per_minute,
            )
            bars_provider = MarketDataBarsProvider(market_data_service=market_data_service)
            cards_provider = ThesisCardHistoryExporter(
                dsn=settings.postgres_dsn,
                thesis_schema=settings.thesis_builder_db_schema,
            )
            repository = BacktesterRepository(
                dsn=settings.postgres_dsn,
                backtester_schema=settings.db_schema,
                market_data_schema=settings.market_data_db_schema,
                thesis_builder_schema=settings.thesis_builder_db_schema,
                shared_schema=settings.shared_db_schema,
            )
            repository.bootstrap_schema(repo_root=_repo_root())
            BacktesterService(
                settings=settings,
                repository=repository,
                cards_provider=cards_provider,
                bars_provider=bars_provider,
            ).run(params)
        except Exception:
            LOGGER.exception("backtester run failed run_id=%s", params.run_id)
        finally:
            with self._lock:
                if self._active_run_id == params.run_id:
                    self._active_run_id = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]
