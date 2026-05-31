from __future__ import annotations

import logging
import os
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg

from .env_loader import load_env_files
from .providers import FinnhubProvider, MarketauxProvider
from .config_sync import sync_seed_configs
from .service import build_service
from .settings import NewsFetcherSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("news_fetcher_runner")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bootstrap_database_schema(settings: NewsFetcherSettings) -> None:
    schema_files = (
        _repo_root() / "src" / "product-components" / "shared" / "db" / "schema.sql",
        _repo_root() / "src" / "product-components" / "news-fetcher" / "db" / "schema.sql",
    )

    with closing(psycopg.connect(settings.postgres_dsn)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for schema_file in schema_files:
                cursor.execute(schema_file.read_text(encoding="utf-8"))


def _build_providers() -> dict[str, Any]:
    providers: dict[str, Any] = {}

    finnhub_enabled = _bool_env("NEWS_SOURCE_FINNHUB_ENABLED", default=True)
    finnhub_key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if finnhub_enabled and finnhub_key:
        providers["finnhub"] = FinnhubProvider(api_key=finnhub_key)

    marketaux_enabled = _bool_env("NEWS_SOURCE_MARKETAUX_ENABLED", default=True)
    marketaux_key = (os.getenv("MARKETAUX_API_KEY") or "").strip()
    if marketaux_enabled and marketaux_key:
        providers["marketaux"] = MarketauxProvider(api_key=marketaux_key)

    return providers


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def main() -> None:
    load_env_files(_repo_root(), override_existing=False)

    settings = NewsFetcherSettings.from_env()
    _bootstrap_database_schema(settings)
    sync_seed_configs(repo_root=_repo_root(), settings=settings)
    providers = _build_providers()

    if not providers and not settings.rss_enabled:
        raise SystemExit(
            "No providers configured. Set FINNHUB_API_KEY, RSS_FEED_URLS, MARKETAUX_API_KEY, or enable RSS DB sources."
        )

    service = build_service(settings=settings, providers=providers)
    interval_seconds = max(
        30,
        min(
            settings.news_poll_interval,
            settings.rss_poll_interval,
            settings.marketaux_poll_interval,
        ),
    )

    LOGGER.info(
        "Starting news-fetcher with providers=%s interval=%ss",
        list(providers.keys()),
        interval_seconds,
    )

    while True:
        try:
            results = service.run_once()
            summary = {
                source: {
                    "fetched": result.fetched,
                    "accepted": result.accepted,
                    "rejected": result.rejected,
                    "checkpoint_advanced": result.checkpoint_advanced,
                }
                for source, result in results.items()
            }
            LOGGER.info("Cycle result: %s", summary)
        except Exception:
            LOGGER.exception("Top-level fetch cycle failure")

        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
