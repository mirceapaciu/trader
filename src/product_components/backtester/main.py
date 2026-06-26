from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.product_components.market_data.service import MarketDataService
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter
from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.shared.adapters import (
    PostgresSharedApiUsageWriter,
    PostgresSharedInstrumentRegistry,
)
from src.product_components.thesis_builder.export import ThesisCardHistoryExporter

from .clients import MarketDataBarsProvider
from .models import (
    BacktestMode,
    BacktestRunParams,
    CardPopulation,
    ExecutionModel,
    RiskModel,
    TimingScenario,
)
from .repository import BacktesterRepository
from .service import BacktesterService, new_run_id
from .settings import BacktesterSettings

LOGGER = logging.getLogger("backtester")


def main() -> None:
    repo_root = _repo_root()
    load_env_files(
        repo_root,
        filenames=(
            ".env.shared",
            ".env.prod",
            ".env.market-data",
            ".env.thesis-builder",
            ".env.backtester",
            ".env.secrets",
        ),
        override_existing=False,
    )
    args = _parse_args()
    settings = BacktesterSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    market_data_settings = MarketDataSettings.from_env()
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
        provider_clients={},
        quote_max_age_seconds=market_data_settings.quote_max_age_seconds,
        daily_bar_lookback_days=market_data_settings.daily_bar_lookback_days,
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
    repository.bootstrap_schema(repo_root=repo_root)

    params = BacktestRunParams(
        run_id=args.run_id or new_run_id(),
        window_start_at=_parse_utc(args.window_start_at),
        window_end_at=_parse_utc(args.window_end_at),
        mode=BacktestMode(args.mode or settings.default_mode),
        timing_scenario=TimingScenario(
            args.timing_scenario or settings.default_timing_scenario
        ),
        card_population=CardPopulation(
            args.card_population or settings.default_card_population
        ),
        strategies=_parse_strategies(args.strategies),
        initial_capital=(
            args.initial_capital
            if args.initial_capital is not None
            else settings.initial_capital_usd
        ),
        ideal_fetch_delay_seconds=(
            args.ideal_fetch_delay_seconds
            if args.ideal_fetch_delay_seconds is not None
            else settings.ideal_fetch_delay_seconds
        ),
        ideal_thesis_delay_seconds=(
            args.ideal_thesis_delay_seconds
            if args.ideal_thesis_delay_seconds is not None
            else settings.ideal_thesis_delay_seconds
        ),
        execution_model=_execution_model_from_settings(settings),
        risk_model=_risk_model_from_settings(settings),
        risk_free_rate_per_period=_risk_free_per_period(settings),
        run_note=args.run_note,
    )
    LOGGER.info("starting backtester run_id=%s", params.run_id)
    BacktesterService(
        settings=settings,
        repository=repository,
        cards_provider=cards_provider,
        bars_provider=bars_provider,
    ).run(params)
    print(json.dumps({"run_id": params.run_id, "status": "completed"}, sort_keys=True))


def _execution_model_from_settings(settings: BacktesterSettings) -> ExecutionModel:
    return ExecutionModel(
        commission_model=settings.commission_model,
        commission_per_share_usd=settings.commission_per_share_usd,
        commission_min_usd=settings.commission_min_usd,
        entry_slippage_bps=settings.entry_slippage_bps,
        exit_slippage_bps=settings.exit_slippage_bps,
        limit_order_validity_bars=settings.limit_order_validity_bars,
        intrabar_stop_before_target=settings.intrabar_stop_before_target,
        bar_interval=settings.bar_interval,
    )


def _risk_model_from_settings(settings: BacktesterSettings) -> RiskModel:
    return RiskModel(
        max_position_usd=settings.max_position_usd,
        max_portfolio_exposure_usd=settings.max_portfolio_exposure_usd,
        max_positions_per_sector=settings.max_positions_per_sector,
        max_daily_trades=settings.max_daily_trades,
        daily_loss_limit_usd=settings.daily_loss_limit_usd,
        ticker_cooldown_minutes=settings.ticker_cooldown_minutes,
    )


def _risk_free_per_period(settings: BacktesterSettings) -> float:
    periods = {"daily": 252, "weekly": 52, "monthly": 12}.get(
        settings.sharpe_periodicity.lower(), 252
    )
    return settings.risk_free_rate_annual / periods


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backtester")
    parser.add_argument("--run-id")
    parser.add_argument("--window-start-at", required=True)
    parser.add_argument("--window-end-at", required=True)
    parser.add_argument("--mode")
    parser.add_argument("--timing-scenario")
    parser.add_argument("--card-population")
    parser.add_argument("--strategies")
    parser.add_argument("--initial-capital", type=float)
    parser.add_argument("--ideal-fetch-delay-seconds", type=int)
    parser.add_argument("--ideal-thesis-delay-seconds", type=int)
    parser.add_argument("--run-note")
    return parser.parse_args()


def _parse_strategies(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_utc(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
