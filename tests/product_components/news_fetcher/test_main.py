from __future__ import annotations

import logging
from dataclasses import replace

from src.product_components.news_fetcher.main import _configure_logging
from src.product_components.news_fetcher.settings import NewsFetcherSettings


def _settings() -> NewsFetcherSettings:
    return NewsFetcherSettings(
        newsfetcher_db_schema="news_fetcher",
        shared_db_schema="shared",
        watchlist_table="t_watchlist_tickers",
        news_poll_interval=120,
        rss_poll_interval=300,
        marketaux_poll_interval=300,
        provider_timeout_seconds=5,
        provider_max_retries=3,
        provider_backoff_base_seconds=1,
        rss_enabled=True,
        rss_rate_limit_backoff_seconds=900,
        instruments_config="",
        rss_sources_config="",
        legacy_rss_feed_urls=(),
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        publish_retry_drain_batch_size=500,
        dedupe_lookback_hours=24,
        dedupe_similarity_threshold=0.9,
        dedupe_algorithm="rapidfuzz_ratio",
        include_keywords=(),
        exclude_keywords=(),
    )


def test_configure_logging_writes_to_configured_log_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "news-fetcher.log"
    settings = replace(_settings(), log_file=str(log_file), log_level="INFO")

    try:
        _configure_logging(settings, tmp_path)
        logging.getLogger("news_fetcher_runner").info("news fetcher log file smoke test")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert "news fetcher log file smoke test" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.basicConfig(handlers=[], force=True)
