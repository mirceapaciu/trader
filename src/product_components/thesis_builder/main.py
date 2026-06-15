from __future__ import annotations

import logging
import sys
from contextlib import closing
from pathlib import Path

import psycopg

from src.product_components.market_data.service import MarketDataService
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter
from src.product_components.news_fetcher.env_loader import load_env_files

from .service import ThesisBuilderRunner
from .settings import ThesisBuilderSettings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bootstrap_database_schema(settings: ThesisBuilderSettings) -> None:
    schema_files = (
        _repo_root() / "src" / "product_components" / "shared" / "db" / "schema.sql",
        _repo_root() / "src" / "product_components" / "news_fetcher" / "db" / "schema.sql",
        _repo_root() / "src" / "product_components" / "market_data" / "db" / "schema.sql",
        _repo_root() / "src" / "product_components" / "thesis_builder" / "db" / "schema.sql",
    )

    with closing(psycopg.connect(settings.postgres_dsn)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for schema_file in schema_files:
                cursor.execute(schema_file.read_text(encoding="utf-8"))


def _configure_logging(settings: ThesisBuilderSettings, repo_root: Path) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = settings.log_file_path(repo_root)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def main() -> None:
    repo_root = _repo_root()
    load_env_files(
        repo_root,
        filenames=(
            ".env.shared",
            ".env.prod",
            ".env.news-fetcher",
            ".env.market-data",
            ".env.thesis-builder",
            ".env.secrets",
        ),
        override_existing=False,
    )

    settings = ThesisBuilderSettings.from_env()
    market_data_settings = MarketDataSettings.from_env()
    _configure_logging(settings, repo_root)
    _bootstrap_database_schema(settings)

    market_data_service = MarketDataService(
        storage=PostgresMarketDataStorageAdapter(
            dsn=market_data_settings.postgres_dsn,
            market_data_schema=market_data_settings.market_data_db_schema,
            shared_schema=market_data_settings.shared_db_schema,
            watchlist_table=market_data_settings.watchlist_table,
        ),
        provider_clients={},
        quote_max_age_seconds=market_data_settings.quote_max_age_seconds,
        daily_bar_lookback_days=market_data_settings.daily_bar_lookback_days,
    )

    ThesisBuilderRunner(
        settings=settings,
        market_context_client=market_data_service,
    ).run_forever()


if __name__ == "__main__":
    main()
