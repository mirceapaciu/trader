from __future__ import annotations

from datetime import UTC, datetime

import feedparser

from src.product_components.news_fetcher.providers import (
    FinnhubProvider,
    MarketauxProvider,
    RssProvider,
)


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_finnhub_provider_maps_payload(monkeypatch) -> None:
    payload = [
        {
            "id": 101,
            "datetime": 1748342400,
            "headline": "Apple beats expectations",
            "summary": "Quarterly results exceeded estimates",
            "url": "https://example.com/finnhub/101",
            "related": "AAPL,MSFT",
        }
    ]

    def _fake_get(*args, **kwargs):
        return _FakeResponse(payload)

    monkeypatch.setattr("requests.get", _fake_get)

    provider = FinnhubProvider(api_key="key")
    batch = provider.fetch(source_key="finnhub", cursor={"min_id": 100}, timeout_seconds=5)

    assert len(batch.events) == 1
    assert batch.events[0].source == "finnhub"
    assert batch.events[0].tickers == ["AAPL", "MSFT"]
    assert batch.next_cursor["min_id"] == 101


def test_rss_provider_filters_using_cursor(monkeypatch) -> None:
    parsed = feedparser.FeedParserDict(
        entries=[
            feedparser.FeedParserDict(
                id="one",
                title="New article",
                link="https://example.com/rss/new",
                summary="Summary",
                published="Wed, 28 May 2026 00:00:00 GMT",
            ),
            feedparser.FeedParserDict(
                id="old",
                title="Old article",
                link="https://example.com/rss/old",
                summary="Summary",
                published="Tue, 27 May 2026 00:00:00 GMT",
            ),
        ]
    )

    def _fake_parse(url):
        return parsed

    monkeypatch.setattr(feedparser, "parse", _fake_parse)

    provider = RssProvider(feed_urls=["https://example.com/feed.xml"])
    batch = provider.fetch(
        source_key="rss:example",
        cursor={"published_after": "2026-05-27T12:00:00Z"},
        timeout_seconds=5,
    )

    assert len(batch.events) == 1
    assert batch.events[0].provider_event_id == "one"
    assert batch.next_cursor["published_after"].startswith("2026-05-28T00:00:00")


def test_marketaux_provider_maps_entities_and_sentiment(monkeypatch) -> None:
    payload = {
        "data": [
            {
                "uuid": "mx-1",
                "published_at": "2026-05-27T11:30:00Z",
                "title": "Guidance upgrade",
                "description": "Company raised outlook",
                "url": "https://example.com/mx/1",
                "entities": [{"symbol": "SAP"}, {"symbol": "OR"}],
                "sentiment": 0.6,
            }
        ]
    }

    def _fake_get(*args, **kwargs):
        return _FakeResponse(payload)

    monkeypatch.setattr("requests.get", _fake_get)

    provider = MarketauxProvider(api_key="key")
    batch = provider.fetch(
        source_key="marketaux",
        cursor={"published_after": "2026-05-27T10:00:00Z"},
        timeout_seconds=5,
    )

    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.source == "marketaux"
    assert event.provider_event_id == "mx-1"
    assert event.tickers == ["SAP", "OR"]
    assert event.sentiment_source == 0.6
    assert batch.cursor_updated_at == datetime(2026, 5, 27, 11, 30, tzinfo=UTC)
