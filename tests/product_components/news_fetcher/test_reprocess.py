from __future__ import annotations

from datetime import datetime, timezone

from src.core_components.event_ingestion_engine.models import FilterOutcome, FilterResult
from src.product_components.filter_quality_evaluator.models import InputArticle
from src.product_components.news_fetcher.filter_config import NewsFilterConfig
from src.product_components.news_fetcher.reprocess import (
    NewsFetcherRejectedArticleReprocessor,
    ReprocessArticleCandidate,
)
from src.product_components.news_fetcher.settings import NewsFetcherSettings


class InMemoryReprocessStorage:
    def __init__(self) -> None:
        self.watchlist = {"AAPL"}
        self.config = NewsFilterConfig(
            filter_config_id="prod_cfg",
            config_name="Production filter",
            config_role="production",
            status="active",
            include_keywords=(),
            exclude_keywords=(),
            watchlist_tickers=(),
            dedupe_algorithm="rapidfuzz_ratio",
            dedupe_similarity_threshold=0.9,
            dedupe_lookback_hours=24,
        )
        self.candidates: list[ReprocessArticleCandidate] = []
        self.context: list[InputArticle] = []
        self.persisted_results: list[FilterResult] = []
        self.persisted_snapshot: dict | None = None
        self.queued_existing_ids: set[str] = set()

    def load_active_watchlist_tickers(self) -> set[str]:
        return set(self.watchlist)

    def seed_production_filter_config_if_missing(self, **_: object) -> NewsFilterConfig:
        return self.config

    def load_reprocess_rejected_articles(self, *, fetched_since, fetched_until):
        return list(self.candidates)

    def load_reprocess_dedupe_context_articles(self, *, published_since, published_until):
        return list(self.context)

    def persist_reprocess_rejected_results(self, *, filter_run, candidates, results) -> int:
        self.persisted_snapshot = filter_run.filter_config_snapshot_json
        self.persisted_results = list(results)
        queued = 0
        for result in results:
            if result.outcome == FilterOutcome.ACCEPTED and result.article_id not in self.queued_existing_ids:
                queued += 1
                self.queued_existing_ids.add(result.article_id)
        return queued


def test_reprocess_accepts_rejected_article_after_active_watchlist_change() -> None:
    storage = InMemoryReprocessStorage()
    storage.watchlist = {"NVDA"}
    storage.candidates = [
        ReprocessArticleCandidate(
            article=_article("a1", "NVIDIA expands Blackwell production", tickers=["NVDA"]),
            source_key="finnhub",
        )
    ]
    service = NewsFetcherRejectedArticleReprocessor(settings=_settings(), storage=storage)

    result = service.reprocess_window(fetched_since=_dt(9), fetched_until=_dt(10))

    assert result.scanned_rejected_count == 1
    assert result.newly_accepted_count == 1
    assert result.queued_publication_obligation_count == 1
    assert storage.persisted_results[0].outcome == FilterOutcome.ACCEPTED
    assert storage.persisted_snapshot is not None
    assert storage.persisted_snapshot["watchlist_tickers"] == ["NVDA"]


def test_reprocess_records_still_rejected_article_without_queueing() -> None:
    storage = InMemoryReprocessStorage()
    storage.candidates = [
        ReprocessArticleCandidate(
            article=_article("a1", "General market commentary", tickers=[]),
            source_key="rss:static",
        )
    ]
    service = NewsFetcherRejectedArticleReprocessor(settings=_settings(), storage=storage)

    result = service.reprocess_window(fetched_since=_dt(9), fetched_until=_dt(10))

    assert result.newly_accepted_count == 0
    assert result.still_rejected_count == 1
    assert result.queued_publication_obligation_count == 0
    assert storage.persisted_results[0].rejection_reason_code == "rejected_not_relevant"


def test_reprocess_skips_rejected_rows_that_are_already_accepted_or_published() -> None:
    storage = InMemoryReprocessStorage()
    storage.candidates = [
        ReprocessArticleCandidate(
            article=_article("accepted", "Apple raises guidance", tickers=["AAPL"]),
            source_key="finnhub",
            already_accepted=True,
        ),
        ReprocessArticleCandidate(
            article=_article("published", "Apple expands production", tickers=["AAPL"]),
            source_key="finnhub",
            already_published=True,
        ),
    ]
    service = NewsFetcherRejectedArticleReprocessor(settings=_settings(), storage=storage)

    result = service.reprocess_window(fetched_since=_dt(9), fetched_until=_dt(10))

    assert result.scanned_rejected_count == 2
    assert result.already_accepted_count == 1
    assert result.already_published_count == 1
    assert result.queued_publication_obligation_count == 0
    assert storage.persisted_results == []


def test_reprocess_preserves_dedupe_rejection_against_existing_accepted_article() -> None:
    storage = InMemoryReprocessStorage()
    storage.config = NewsFilterConfig(
        filter_config_id="prod_cfg",
        config_name="Production filter",
        config_role="production",
        status="active",
        include_keywords=(),
        exclude_keywords=(),
        watchlist_tickers=("AAPL",),
        dedupe_algorithm="rapidfuzz_ratio",
        dedupe_similarity_threshold=0.7,
        dedupe_lookback_hours=24,
    )
    storage.context = [_article("accepted", "Apple raises quarterly guidance", tickers=["MSFT"])]
    storage.candidates = [
        ReprocessArticleCandidate(
            article=_article("dupe", "Apple raises quarterly guidance", tickers=["AAPL"]),
            source_key="finnhub",
        )
    ]
    service = NewsFetcherRejectedArticleReprocessor(settings=_settings(), storage=storage)

    result = service.reprocess_window(fetched_since=_dt(9), fetched_until=_dt(10))

    assert result.newly_accepted_count == 0
    assert result.still_rejected_count == 1
    assert storage.persisted_results[0].rejection_reason_code == "rejected_soft_duplicate"


def _article(article_id: str, headline: str, *, tickers: list[str]) -> InputArticle:
    return InputArticle(
        id=article_id,
        source="finnhub",
        headline=headline,
        summary=None,
        url=f"https://example.test/{article_id}",
        tickers=tickers,
        published_at=_dt(9),
        fetched_at=_dt(9),
        sentiment_source=None,
    )


def _dt(hour: int) -> datetime:
    return datetime(2026, 7, 11, hour, 0, tzinfo=timezone.utc)


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
        include_keywords=("guidance",),
        exclude_keywords=(),
    )
