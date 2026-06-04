from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from src.core_components.event_ingestion_engine.deduplication import evaluate_dedupe
from src.core_components.event_ingestion_engine.models import (
    FilterOutcome,
    FilterResult,
    SoftDedupePolicy,
)
from src.product_components.news_fetcher.settings import NewsFetcherSettings
from src.product_components.news_fetcher.source_adapter import (
    NormalizedNewsArticle,
    SourceFilterConfig,
    evaluate_relevance,
)

from .models import FilterSimulationConfig, InputArticle


def simulation_config_from_snapshot(snapshot: dict[str, Any]) -> FilterSimulationConfig:
    return FilterSimulationConfig(
        include_keywords=_string_tuple(snapshot.get("include_keywords")),
        exclude_keywords=_string_tuple(snapshot.get("exclude_keywords")),
        watchlist_tickers={value.upper() for value in _string_tuple(snapshot.get("watchlist_tickers"))},
        dedupe_algorithm=str(snapshot.get("dedupe_algorithm") or "rapidfuzz_ratio"),
        dedupe_similarity_threshold=float(snapshot.get("dedupe_similarity_threshold", 0.9)),
        dedupe_lookback_hours=int(snapshot.get("dedupe_lookback_hours", 24)),
    )


def simulation_config_from_news_settings(
    settings: NewsFetcherSettings,
    *,
    watchlist_tickers: set[str],
) -> FilterSimulationConfig:
    return FilterSimulationConfig(
        include_keywords=settings.include_keywords,
        exclude_keywords=settings.exclude_keywords,
        watchlist_tickers=set(watchlist_tickers),
        dedupe_algorithm=settings.dedupe_algorithm,
        dedupe_similarity_threshold=settings.dedupe_similarity_threshold,
        dedupe_lookback_hours=settings.dedupe_lookback_hours,
    )


def fingerprint_for_snapshot(snapshot: dict[str, Any]) -> str:
    normalized = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def simulate_filter_results(
    articles: list[InputArticle],
    *,
    config: FilterSimulationConfig,
) -> list[FilterResult]:
    accepted_events = []
    results: list[FilterResult] = []
    source_filter = SourceFilterConfig(
        watchlist_tickers=config.watchlist_tickers,
        include_keywords=config.include_keywords,
        exclude_keywords=config.exclude_keywords,
    )
    dedupe_policy = SoftDedupePolicy(
        enabled=True,
        algorithm=config.dedupe_algorithm,
        threshold=config.dedupe_similarity_threshold,
        lookback_window=timedelta(hours=config.dedupe_lookback_hours),
        max_time_delta_hours=config.dedupe_lookback_hours,
    )

    for article in sorted(articles, key=lambda item: (item.published_at, item.id)):
        normalized = NormalizedNewsArticle(
            source=article.source,
            provider_event_id=article.id,
            headline=article.headline,
            summary=article.summary,
            url=article.url,
            tickers=article.tickers,
            published_at=article.published_at,
            fetched_at=article.fetched_at,
            sentiment_source=article.sentiment_source,
        )
        pre_filter_outcome, reason = evaluate_relevance(normalized, source_filter)
        event = article.canonical_event()
        if pre_filter_outcome == FilterOutcome.REJECTED.value:
            results.append(
                FilterResult(
                    article_id=article.id,
                    outcome=FilterOutcome.REJECTED,
                    rejection_reason_code=reason,
                )
            )
            continue

        decision = evaluate_dedupe(event, list(accepted_events), dedupe_policy)
        if decision.accepted:
            accepted_events.append(event)
            results.append(FilterResult(article_id=article.id, outcome=FilterOutcome.ACCEPTED))
            continue

        results.append(
            FilterResult(
                article_id=article.id,
                outcome=FilterOutcome.REJECTED,
                rejection_reason_code=decision.reason_code,
                matched_article_id=decision.matched_event_id,
                similarity_score=decision.similarity_score,
                details=dict(decision.audit),
            )
        )

    return results


def deterministic_sample(items: list[Any], *, run_id: str, sample_size: int) -> list[Any]:
    if sample_size >= len(items):
        return list(items)
    ranked = sorted(
        items,
        key=lambda item: (
            hashlib.sha256(f"{run_id}:{item.article.id}".encode("utf-8")).hexdigest(),
            item.article.id,
        ),
    )
    return ranked[:sample_size]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item).strip().lower() for item in value if str(item).strip())
