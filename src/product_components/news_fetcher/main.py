from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from .env_loader import load_env_files
from .providers import FinnhubProvider, MarketauxProvider, RssProvider
from .service import build_service
from .settings import NewsFetcherSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("news_fetcher_runner")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _build_providers() -> dict[str, Any]:
    providers: dict[str, Any] = {}

    finnhub_key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if finnhub_key:
        providers["finnhub"] = FinnhubProvider(api_key=finnhub_key)

    rss_urls = [
        url.strip()
        for url in (os.getenv("RSS_FEED_URLS") or "").split(",")
        if url.strip()
    ]
    if rss_urls:
        providers["rss"] = RssProvider(feed_urls=rss_urls)

    marketaux_key = (os.getenv("MARKETAUX_API_KEY") or "").strip()
    if marketaux_key:
        providers["marketaux"] = MarketauxProvider(api_key=marketaux_key)

    return providers


def main() -> None:
    load_env_files(_repo_root(), override_existing=False)

    settings = NewsFetcherSettings.from_env()
    providers = _build_providers()

    if not providers:
        raise SystemExit(
            "No providers configured. Set FINNHUB_API_KEY, RSS_FEED_URLS, or MARKETAUX_API_KEY."
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
