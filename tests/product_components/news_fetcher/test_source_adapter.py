from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.news_fetcher.providers import (
    NewsProvider,
    ProviderArticle,
    ProviderBatch,
)
from src.product_components.news_fetcher.source_adapter import (
    NewsSourceAdapter,
    SourceFilterConfig,
)


class FakeProvider(NewsProvider):
    def __init__(self, batch: ProviderBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[str, Any, int]] = []

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        self.calls.append((source_key, cursor, timeout_seconds))
        return self.batch


def _article(**kwargs) -> ProviderArticle:
    base = ProviderArticle(
        source="finnhub",
        headline="Company A beats guidance",
        url="https://example.com/news/earnings?utm_source=x",
        published_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 27, 12, 1, tzinfo=timezone.utc),
        summary="Strong quarterly revenue growth",
        tickers=["aapl", "AAPL", ""],
        sentiment_source=0.42,
        provider_event_id="evt-1",
    )
    return ProviderArticle(**{**base.__dict__, **kwargs})


def test_source_adapter_filters_and_maps_relevant_articles() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[
                _article(),
                _article(
                    headline="Ignore this paid promotion",
                    tickers=[],
                    summary="Sponsored content",
                    provider_event_id="evt-2",
                ),
            ],
            next_cursor={"cursor": "next"},
            cursor_updated_at=datetime(2026, 5, 27, 12, 2, tzinfo=timezone.utc),
        )
    )
    adapter = NewsSourceAdapter(
        provider=provider,
        timeout_seconds=9,
        filter_config=SourceFilterConfig(
            watchlist_tickers={"AAPL"},
            include_keywords=("earnings",),
            exclude_keywords=("sponsored", "paid promotion"),
        ),
    )

    batch = adapter.fetch("finnhub", {"cursor": "old"})

    assert provider.calls == [("finnhub", {"cursor": "old"}, 9)]
    assert batch.next_cursor == {"cursor": "next"}
    assert len(batch.events) == 1

    event = batch.events[0]
    assert event.source == "finnhub"
    assert event.canonical_locator.startswith("https://example.com/news/earnings")
    assert event.entities == ["AAPL"]
    assert event.attributes["sentiment_source"] == 0.42


def test_source_adapter_drops_invalid_structural_records() -> None:
    provider = FakeProvider(
        ProviderBatch(
            events=[
                _article(url="not-a-url"),
                _article(headline="   "),
            ],
            next_cursor=None,
            cursor_updated_at=datetime(2026, 5, 27, 12, 2, tzinfo=timezone.utc),
        )
    )
    adapter = NewsSourceAdapter(
        provider=provider,
        timeout_seconds=9,
        filter_config=SourceFilterConfig(
            watchlist_tickers={"AAPL"},
            include_keywords=("earnings",),
            exclude_keywords=(),
        ),
    )

    batch = adapter.fetch("finnhub", None)

    assert batch.events == []
