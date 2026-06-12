from __future__ import annotations

from datetime import UTC, datetime

import feedparser
import pytest
import requests

from src.product_components.news_fetcher.providers import (
    FinnhubProvider,
    MarketauxProvider,
    ProviderRateLimitError,
    RssProvider,
)
from src.product_components.news_fetcher.rss_feeds import RssFeedSpec
from src.product_components.news_fetcher.rss_feeds import RssTickerMatch


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.content = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("error", response=self)
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.trust_env = True
        self.calls = []

    def get(self, url, *, params, timeout, headers=None):
        self.calls.append((url, params, timeout, self.trust_env, headers))
        return _FakeResponse(self.payload, self.status_code)


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

    fake_session = _FakeSession(payload)
    monkeypatch.setattr("requests.Session", lambda: fake_session)

    provider = FinnhubProvider(api_key="key")
    batch = provider.fetch(source_key="finnhub", cursor={"min_id": 100}, timeout_seconds=5)

    assert len(batch.events) == 1
    assert batch.events[0].source == "finnhub"
    assert batch.events[0].tickers == ["AAPL", "MSFT"]
    assert batch.next_cursor["min_id"] == 101
    assert fake_session.calls[0][3] is False


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

    def _fake_parse(feed_url, *, timeout_seconds):
        return parsed

    monkeypatch.setattr(
        "src.product_components.news_fetcher.providers._parse_rss_feed",
        _fake_parse,
    )

    provider = RssProvider(feed_urls=["https://example.com/feed.xml"])
    batch = provider.fetch(
        source_key="rss:example",
        cursor={"published_after": "2026-05-27T12:00:00Z"},
        timeout_seconds=5,
    )

    assert len(batch.events) == 1
    assert batch.events[0].provider_event_id == "one"
    assert batch.next_cursor["published_after"].startswith("2026-05-28T00:00:00")


def test_rss_provider_fetches_with_proxy_env_disabled(monkeypatch) -> None:
    rss_payload = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <guid>rss-1</guid>
      <title>Market sentiment improves</title>
      <link>https://example.com/rss/1</link>
      <pubDate>Wed, 28 May 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

    fake_session = _FakeSession(rss_payload)
    monkeypatch.setattr("requests.Session", lambda: fake_session)

    provider = RssProvider(feed_urls=["https://example.com/feed.xml"])
    batch = provider.fetch(source_key="rss:example", cursor=None, timeout_seconds=5)

    assert len(batch.events) == 1
    assert batch.events[0].provider_event_id == "rss-1"
    assert fake_session.calls[0][3] is False
    assert fake_session.calls[0][4]["User-Agent"].startswith("Mozilla/5.0")
    assert "application/rss+xml" in fake_session.calls[0][4]["Accept"]


def test_rss_provider_tags_articles_with_feed_spec_tickers(monkeypatch) -> None:
    parsed = feedparser.FeedParserDict(
        entries=[
            feedparser.FeedParserDict(
                id="sony-1",
                title="Sony raises outlook",
                link="https://example.com/rss/sony",
                summary="Guidance improved",
                published="Wed, 28 May 2026 00:00:00 GMT",
            )
        ]
    )

    def _fake_parse(feed_url, *, timeout_seconds):
        return parsed

    monkeypatch.setattr(
        "src.product_components.news_fetcher.providers._parse_rss_feed",
        _fake_parse,
    )

    provider = RssProvider(
        feed_spec=RssFeedSpec(
            source_key="rss:yahoo_finance:SONY:NYSE",
            provider_name="yahoo_finance",
            url="https://feeds.finance.yahoo.com/rss/2.0/headline?s=SONY&region=US&lang=en-US",
            app_tickers=("SONY",),
        )
    )
    batch = provider.fetch(source_key="rss:yahoo_finance:SONY:NYSE", cursor=None, timeout_seconds=5)

    assert len(batch.events) == 1
    assert batch.events[0].tickers == ["SONY"]


def test_rss_provider_tags_grouped_articles_by_match_terms(monkeypatch) -> None:
    parsed = feedparser.FeedParserDict(
        entries=[
            feedparser.FeedParserDict(
                id="nvda-1",
                title="NVIDIA raises guidance",
                link="https://example.com/rss/nvidia",
                summary="Demand improved",
                published="Wed, 28 May 2026 00:00:00 GMT",
            ),
            feedparser.FeedParserDict(
                id="rhm-1",
                title="Rheinmetall wins order",
                link="https://example.com/rss/rheinmetall",
                summary="Defense contract signed",
                published="Wed, 28 May 2026 00:01:00 GMT",
            ),
            feedparser.FeedParserDict(
                id="macro-1",
                title="European markets edge higher",
                link="https://example.com/rss/markets",
                summary="Broad market sentiment improved",
                published="Wed, 28 May 2026 00:02:00 GMT",
            ),
        ]
    )

    def _fake_parse(feed_url, *, timeout_seconds):
        return parsed

    monkeypatch.setattr(
        "src.product_components.news_fetcher.providers._parse_rss_feed",
        _fake_parse,
    )

    provider = RssProvider(
        feed_spec=RssFeedSpec(
            source_key="rss:yahoo_finance:batch:abc",
            provider_name="yahoo_finance",
            url="https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA%2CRHM.DE&region=US&lang=en-US",
            app_tickers=("NVDA", "RHM"),
            ticker_matches=(
                RssTickerMatch(ticker="NVDA", match_terms=("NVDA", "NVIDIA")),
                RssTickerMatch(ticker="RHM", match_terms=("RHM.DE", "Rheinmetall")),
            ),
        )
    )
    batch = provider.fetch(source_key="rss:yahoo_finance:batch:abc", cursor=None, timeout_seconds=5)

    assert [event.tickers for event in batch.events] == [["NVDA"], ["RHM"], []]


def test_rss_provider_raises_rate_limit_error_on_429(monkeypatch) -> None:
    fake_session = _FakeSession(b"", status_code=429)
    monkeypatch.setattr("requests.Session", lambda: fake_session)

    provider = RssProvider(feed_urls=["https://example.com/feed.xml"])

    with pytest.raises(ProviderRateLimitError):
        provider.fetch(source_key="rss:static", cursor=None, timeout_seconds=5)


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

    fake_session = _FakeSession(payload)
    monkeypatch.setattr("requests.Session", lambda: fake_session)

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
    assert fake_session.calls[0][3] is False
