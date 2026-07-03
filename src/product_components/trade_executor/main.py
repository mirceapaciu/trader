from __future__ import annotations

import logging
import sys
from contextlib import closing
from pathlib import Path

LOGGER = logging.getLogger("trade_executor.main")

import psycopg

from src.product_components.market_data.service import MarketDataService
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter
from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.shared.adapters import (
    PostgresSharedApiUsageWriter,
    PostgresSharedInstrumentRegistry,
    PostgresSharedInstrumentSectorReader,
    PostgresSharedThesisCardReviewReader,
)

from .repository import PostgresTradeExecutorRepository
from .service import TradeExecutorRunner
from .settings import ModePortMismatchError, TradeExecutorSettings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bootstrap_database_schema(settings: TradeExecutorSettings) -> None:
    schema_files = (
        _repo_root() / "src" / "product_components" / "shared" / "db" / "schema.sql",
        _repo_root() / "src" / "product_components" / "market_data" / "db" / "schema.sql",
        _repo_root() / "src" / "product_components" / "trade_executor" / "db" / "schema.sql",
    )
    with closing(psycopg.connect(settings.postgres_dsn)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for schema_file in schema_files:
                cursor.execute(schema_file.read_text(encoding="utf-8"))


def _configure_logging(settings: TradeExecutorSettings, repo_root: Path) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = settings.log_file_path(repo_root)
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print(
                f"Unable to open trade-executor log file {log_file}; "
                f"falling back to stderr-only logging: {exc}",
                file=sys.stderr,
            )
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def _verify_configuration(settings: TradeExecutorSettings) -> None:
    """Fail closed unless trading mode and IBKR port agree (behavior.md §5)."""
    try:
        settings.validate_mode_port_agreement()
    except ModePortMismatchError as exc:
        LOGGER.error("Cannot start: %s", exc)
        sys.exit(1)
    LOGGER.info(
        "config trading_mode=%s ibkr_port=%s client_id=%s",
        settings.trading_mode, settings.ibkr_port, settings.ibkr_client_id,
    )


def _log_env_files(repo_root: Path, requested: tuple[str, ...], loaded: list[Path]) -> None:
    loaded_names = {p.name for p in loaded}
    for name in requested:
        if name in loaded_names:
            LOGGER.info("env file loaded: %s", repo_root / name)
        else:
            LOGGER.warning("env file not found: %s", repo_root / name)


def _build_market_data_service(settings: TradeExecutorSettings) -> MarketDataService:
    market_data_settings = MarketDataSettings.from_env()
    instrument_registry = PostgresSharedInstrumentRegistry(
        dsn=market_data_settings.postgres_dsn,
        shared_schema=market_data_settings.shared_db_schema,
        watchlist_table=market_data_settings.watchlist_table,
    )
    return MarketDataService(
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


def main() -> None:
    repo_root = _repo_root()
    env_filenames = (".env.shared", ".env.prod", ".env.trade-executor", ".env.secrets")
    loaded_env_files = load_env_files(repo_root, filenames=env_filenames, override_existing=True)

    settings = TradeExecutorSettings.from_env()
    _configure_logging(settings, repo_root)
    _log_env_files(repo_root, env_filenames, loaded_env_files)
    _verify_configuration(settings)
    _bootstrap_database_schema(settings)

    # Imported here (not at module top) so the ib_async dependency is only pulled
    # in for the real run, keeping unit tests of this module import-light.
    from .broker.ib_async_gateway import IbAsyncBrokerGateway

    broker = IbAsyncBrokerGateway(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
    )

    watchlist_registry = PostgresSharedInstrumentRegistry(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
        watchlist_table=settings.watchlist_table,
    )

    TradeExecutorRunner(
        settings=settings,
        broker=broker,
        repository=PostgresTradeExecutorRepository(
            dsn=settings.postgres_dsn, schema=settings.trade_executor_db_schema
        ),
        market_context_client=_build_market_data_service(settings),
        review_reader=PostgresSharedThesisCardReviewReader(
            dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema
        ),
        watchlist_reader=watchlist_registry,
        sector_reader=PostgresSharedInstrumentSectorReader(
            dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema
        ),
    ).run_forever()


if __name__ == "__main__":
    main()
