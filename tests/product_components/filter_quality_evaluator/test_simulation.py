from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.filter_quality_evaluator.models import FilterSimulationConfig, InputArticle
from src.product_components.filter_quality_evaluator.simulation import (
    deterministic_sample,
    fingerprint_for_snapshot,
    simulate_filter_results,
)


def _article(article_id: str, headline: str, *, summary: str | None = None, tickers=None) -> InputArticle:
    return InputArticle(
        id=article_id,
        source="finnhub",
        headline=headline,
        summary=summary,
        url=f"https://example.com/{article_id}",
        tickers=list(tickers or []),
        published_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 27, 12, 1, tzinfo=timezone.utc),
        sentiment_source=None,
    )


def _config() -> FilterSimulationConfig:
    return FilterSimulationConfig(
        include_keywords=("guidance",),
        exclude_keywords=("sponsored",),
        watchlist_tickers={"AAPL"},
        dedupe_algorithm="rapidfuzz_ratio",
        dedupe_similarity_threshold=0.9,
        dedupe_lookback_hours=24,
    )


def test_simulate_filter_results_applies_relevance_rules() -> None:
    results = simulate_filter_results(
        [
            _article("a1", "Apple raises outlook", tickers=["AAPL"]),
            _article("a2", "Sponsored market note", summary="Sponsored", tickers=[]),
            _article("a3", "Generic market update", tickers=[]),
        ],
        config=_config(),
    )

    assert [result.outcome.value for result in results] == ["accepted", "rejected", "rejected"]
    assert results[1].rejection_reason_code == "rejected_excluded_keyword"
    assert results[2].rejection_reason_code == "rejected_not_relevant"


def test_fingerprint_for_snapshot_is_order_stable() -> None:
    left = {"b": 2, "a": ["x"]}
    right = {"a": ["x"], "b": 2}

    assert fingerprint_for_snapshot(left) == fingerprint_for_snapshot(right)


def test_deterministic_sample_is_stable() -> None:
    class Item:
        def __init__(self, article_id: str) -> None:
            self.article = type("Article", (), {"id": article_id})()

    items = [Item("a"), Item("b"), Item("c")]

    first = deterministic_sample(items, run_id="run-1", sample_size=2)
    second = deterministic_sample(list(reversed(items)), run_id="run-1", sample_size=2)

    assert [item.article.id for item in first] == [item.article.id for item in second]
