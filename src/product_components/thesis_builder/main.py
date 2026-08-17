from __future__ import annotations

import logging
import sys
from contextlib import closing
from pathlib import Path

LOGGER = logging.getLogger("thesis_builder.main")

import psycopg

from src.product_components.market_data.factory import build_market_data_service
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentRegistry,
    PostgresSharedThesisCardReviewWriter,
)

from .service import ThesisBuilderRunner
from .settings import ThesisBuilderSettings
from .taxonomy_seed import bootstrap_predefined_taxonomy

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
        connection.autocommit = False
        with connection.cursor() as cursor:
            for schema_file in schema_files:
                cursor.execute(schema_file.read_text(encoding="utf-8"))
        bootstrap_predefined_taxonomy(connection)
        connection.commit()


def _configure_logging(settings: ThesisBuilderSettings, repo_root: Path) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = settings.log_file_path(repo_root)
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print(
                f"Unable to open thesis-builder log file {log_file}; falling back to stderr-only logging: {exc}",
                file=sys.stderr,
            )

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def _verify_configuration(settings: ThesisBuilderSettings) -> None:
    mandatory = {"OPENAI_API_KEY": settings.openai_api_key}
    missing = [name for name, value in mandatory.items() if not value]

    for name, value in mandatory.items():
        if value:
            LOGGER.info("config %s: set", name)
        else:
            LOGGER.error("config %s: missing", name)

    if missing:
        LOGGER.error("Cannot start: missing mandatory configuration: %s", ", ".join(missing))
        sys.exit(1)


def _log_env_files(repo_root: Path, requested: tuple[str, ...], loaded: list[Path]) -> None:
    loaded_names = {p.name for p in loaded}
    for name in requested:
        if name in loaded_names:
            LOGGER.info("env file loaded: %s", repo_root / name)
        else:
            LOGGER.warning("env file not found: %s", repo_root / name)


def main() -> None:
    repo_root = _repo_root()
    env_filenames = (".env.shared", ".env.prod", ".env.thesis-builder", ".env.secrets")
    loaded_env_files = load_env_files(repo_root, filenames=env_filenames, override_existing=True)

    settings = ThesisBuilderSettings.from_env()
    market_data_settings = MarketDataSettings.from_env()
    _configure_logging(settings, repo_root)
    _log_env_files(repo_root, env_filenames, loaded_env_files)
    _verify_configuration(settings)
    _bootstrap_database_schema(settings)
    instrument_registry = PostgresSharedInstrumentRegistry(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
        watchlist_table=market_data_settings.watchlist_table,
    )

    # Live quote/bar retrieval chain: DB cache -> IBKR -> Polygon. The gateway connect is
    # best-effort; when IBKR is down the service transparently falls back to Polygon.
    market_data_service, ibkr_gateway = build_market_data_service(
        market_data_settings, instrument_registry=instrument_registry
    )

    try:
        ThesisBuilderRunner(
            settings=settings,
            market_context_client=market_data_service,
            instrument_registry=instrument_registry,
            review_writer=PostgresSharedThesisCardReviewWriter(
                dsn=settings.postgres_dsn,
                shared_schema=settings.shared_db_schema,
            ),
        ).run_forever()
    finally:
        if ibkr_gateway is not None:
            ibkr_gateway.disconnect()


if __name__ == "__main__":
    main()
