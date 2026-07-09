from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from typing import Any, Protocol

import feedparser
import requests
from requests import HTTPError

from src.product_components.shared.text_match import contains_term

from .rss_feeds import RssFeedSpec

_RSS_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


@dataclass(frozen=True)
class ProviderArticle:
    """Raw provider article payload normalized to one internal shape."""

    source: str
    headline: str
    url: str
    published_at: datetime
    fetched_at: datetime
    summary: str | None = None
    tickers: list[str] = field(default_factory=list)
    sentiment_source: float | None = None
    provider_event_id: str | None = None


@dataclass(frozen=True)
class ProviderBatch:
    """One provider fetch result plus cursor metadata."""

    events: list[ProviderArticle]
    next_cursor: Any
    cursor_updated_at: datetime


class NewsProvider(Protocol):
    """Provider integration contract consumed by NewsFetcher."""

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        """Fetch one batch from provider using a replay-safe cursor."""


class ProviderRateLimitError(RuntimeError):
    """Raised when an upstream provider explicitly rate-limits a request."""


class FinnhubProvider:
    """Fetches market headlines from Finnhub market news endpoint."""

    def __init__(self, *, api_key: str, category: str = "general") -> None:
        if not api_key.strip():
            raise ValueError("FINNHUB_API_KEY is required for FinnhubProvider")
        self._api_key = api_key.strip()
        self._category = category.strip() or "general"

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        min_id = _extract_int_cursor(cursor, "min_id")
        params: dict[str, Any] = {
            "category": self._category,
            "token": self._api_key,
        }
        if min_id is not None:
            params["minId"] = min_id

        response = _http_get(
            "https://finnhub.io/api/v1/news",
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else []

        fetched_at = _utc_now()
        events: list[ProviderArticle] = []
        max_id = min_id
        max_published_at: datetime | None = None

        for item in items:
            if not isinstance(item, dict):
                continue
            published_at = _parse_unix_seconds(item.get("datetime"))
            if published_at is None:
                continue

            related = str(item.get("related") or "")
            tickers = [ticker.strip().upper() for ticker in related.split(",") if ticker.strip()]

            article_id = item.get("id")
            normalized_id = str(article_id) if article_id is not None else None

            events.append(
                ProviderArticle(
                    source="finnhub",
                    headline=str(item.get("headline") or ""),
                    summary=str(item.get("summary") or "") or None,
                    url=str(item.get("url") or ""),
                    tickers=tickers,
                    sentiment_source=None,
                    provider_event_id=normalized_id,
                    published_at=published_at,
                    fetched_at=fetched_at,
                )
            )

            if isinstance(article_id, int):
                max_id = article_id if max_id is None else max(max_id, article_id)
            if max_published_at is None or published_at > max_published_at:
                max_published_at = published_at

        next_cursor = {
            "min_id": max_id,
            "cursor_updated_at": _isoformat(max_published_at or fetched_at),
        }
        return ProviderBatch(
            events=events,
            next_cursor=next_cursor,
            cursor_updated_at=max_published_at or fetched_at,
        )


class FinnhubCompanyNewsProvider:
    """Fetches per-symbol company news from the Finnhub company-news endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        ticker: str,
        provider_symbol: str | None = None,
        bootstrap_lookback_hours: int = 24,
    ) -> None:
        if not api_key.strip():
            raise ValueError("FINNHUB_API_KEY is required for FinnhubCompanyNewsProvider")
        normalized_ticker = ticker.strip().upper()
        if not normalized_ticker:
            raise ValueError("ticker is required for FinnhubCompanyNewsProvider")
        self._api_key = api_key.strip()
        self._ticker = normalized_ticker
        self._symbol = (provider_symbol or normalized_ticker).strip().upper()
        self._bootstrap_lookback_hours = bootstrap_lookback_hours

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        since = _extract_datetime_cursor(cursor, "published_after")
        fetched_at = _utc_now()
        window_start = since if since is not None else fetched_at - timedelta(hours=self._bootstrap_lookback_hours)
        params: dict[str, Any] = {
            "symbol": self._symbol,
            "from": window_start.date().isoformat(),
            "to": fetched_at.date().isoformat(),
            "token": self._api_key,
        }

        response = _http_get(
            "https://finnhub.io/api/v1/company-news",
            params=params,
            timeout=timeout_seconds,
        )
        try:
            response.raise_for_status()
        except HTTPError as error:
            if response.status_code == 429:
                raise ProviderRateLimitError("provider_rate_limited") from error
            raise
        payload = response.json()
        items = payload if isinstance(payload, list) else []

        events: list[ProviderArticle] = []
        max_published_at = since

        for item in items:
            if not isinstance(item, dict):
                continue
            published_at = _parse_unix_seconds(item.get("datetime"))
            if published_at is None:
                continue
            if since is not None and published_at <= since:
                continue

            related = str(item.get("related") or "")
            tickers = {ticker.strip().upper() for ticker in related.split(",") if ticker.strip()}
            tickers.add(self._ticker)

            article_id = item.get("id")
            events.append(
                ProviderArticle(
                    source="finnhub",
                    headline=str(item.get("headline") or ""),
                    summary=str(item.get("summary") or "") or None,
                    url=str(item.get("url") or ""),
                    tickers=sorted(tickers),
                    sentiment_source=None,
                    provider_event_id=str(article_id) if article_id is not None else None,
                    published_at=published_at,
                    fetched_at=fetched_at,
                )
            )
            if max_published_at is None or published_at > max_published_at:
                max_published_at = published_at

        next_cursor = {
            "published_after": _isoformat(max_published_at or fetched_at),
        }
        return ProviderBatch(
            events=events,
            next_cursor=next_cursor,
            cursor_updated_at=max_published_at or fetched_at,
        )


def finnhub_company_news_source_key(*, ticker: str, exchange_code: str) -> str:
    return f"finnhub:company:{ticker.strip().upper()}:{exchange_code.strip().upper()}"


class RssProvider:
    """Fetches headlines from configured RSS feeds."""

    def __init__(
        self,
        *,
        feed_urls: list[str] | None = None,
        feed_spec: RssFeedSpec | None = None,
    ) -> None:
        specs: list[RssFeedSpec] = []
        if feed_spec is not None:
            specs.append(feed_spec)
        specs.extend(
            RssFeedSpec(
                source_key="rss:static",
                provider_name="static",
                url=url.strip(),
                app_tickers=(),
            )
            for url in (feed_urls or [])
            if url.strip()
        )
        self._feed_specs = specs
        if not self._feed_specs:
            raise ValueError("RssProvider requires at least one feed URL or feed spec")

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        since = _extract_datetime_cursor(cursor, "published_after")
        fetched_at = _utc_now()
        events: list[ProviderArticle] = []
        max_published_at = since

        for feed_spec in self._feed_specs:
            parsed = _parse_rss_feed(feed_spec.url, timeout_seconds=timeout_seconds)
            entries = getattr(parsed, "entries", [])
            for entry in entries:
                published_at = _parse_rss_datetime(entry)
                if published_at is None:
                    continue
                if since is not None and published_at <= since:
                    continue

                url = str(getattr(entry, "link", "") or "")
                headline = str(getattr(entry, "title", "") or "")
                summary = str(getattr(entry, "summary", "") or "") or None
                tickers = _match_rss_tickers(
                    headline=headline,
                    summary=summary,
                    url=url,
                    feed_spec=feed_spec,
                )
                provider_event_id = (
                    str(getattr(entry, "id", "") or "")
                    or str(getattr(entry, "guid", "") or "")
                    or url
                )
                events.append(
                    ProviderArticle(
                        source="rss",
                        headline=headline,
                        summary=summary,
                        url=url,
                        tickers=tickers,
                        sentiment_source=None,
                        provider_event_id=provider_event_id,
                        published_at=published_at,
                        fetched_at=fetched_at,
                    )
                )
                if max_published_at is None or published_at > max_published_at:
                    max_published_at = published_at

        next_cursor = {
            "published_after": _isoformat(max_published_at or fetched_at),
        }
        return ProviderBatch(
            events=events,
            next_cursor=next_cursor,
            cursor_updated_at=max_published_at or fetched_at,
        )


class MarketauxProvider:
    """Fetches structured news from Marketaux."""

    def __init__(self, *, api_key: str, language: str = "en") -> None:
        if not api_key.strip():
            raise ValueError("MARKETAUX_API_KEY is required for MarketauxProvider")
        self._api_key = api_key.strip()
        self._language = language.strip() or "en"

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        published_after = _extract_datetime_cursor(cursor, "published_after")
        params: dict[str, Any] = {
            "api_token": self._api_key,
            "language": self._language,
            "sort": "published_desc",
            "limit": 100,
        }
        if published_after is not None:
            params["published_after"] = _isoformat(published_after)

        response = _http_get(
            "https://api.marketaux.com/v1/news/all",
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        raw_payload = response.json()
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []

        fetched_at = _utc_now()
        events: list[ProviderArticle] = []
        max_published_at = published_after

        for row in rows:
            if not isinstance(row, dict):
                continue
            published_at = _parse_datetime(row.get("published_at"))
            if published_at is None:
                continue

            entities = row.get("entities") if isinstance(row.get("entities"), list) else []
            tickers = []
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                symbol = str(entity.get("symbol") or "").strip().upper()
                if symbol:
                    tickers.append(symbol)

            sentiment = row.get("sentiment")
            sentiment_source = float(sentiment) if isinstance(sentiment, (int, float)) else None

            events.append(
                ProviderArticle(
                    source="marketaux",
                    headline=str(row.get("title") or ""),
                    summary=str(row.get("description") or "") or None,
                    url=str(row.get("url") or ""),
                    tickers=tickers,
                    sentiment_source=sentiment_source,
                    provider_event_id=str(row.get("uuid") or "") or None,
                    published_at=published_at,
                    fetched_at=fetched_at,
                )
            )
            if max_published_at is None or published_at > max_published_at:
                max_published_at = published_at

        next_cursor = {
            "published_after": _isoformat(max_published_at or fetched_at),
        }
        return ProviderBatch(
            events=events,
            next_cursor=next_cursor,
            cursor_updated_at=max_published_at or fetched_at,
        )


def _extract_int_cursor(cursor: Any | None, key: str) -> int | None:
    if not isinstance(cursor, dict):
        return None
    value = cursor.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _extract_datetime_cursor(cursor: Any | None, key: str) -> datetime | None:
    if not isinstance(cursor, dict):
        return None
    return _parse_datetime(cursor.get(key))


def _parse_unix_seconds(value: Any) -> datetime | None:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, float):
        return datetime.fromtimestamp(int(value), tz=UTC)
    return None


def _parse_rss_datetime(entry: Any) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is not None:
        return datetime(*parsed[:6], tzinfo=UTC)

    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    return _parse_datetime(raw)


def _parse_rss_feed(feed_url: str, *, timeout_seconds: int) -> Any:
    response = _http_get(
        feed_url,
        params={},
        timeout=timeout_seconds,
        headers=_RSS_REQUEST_HEADERS,
    )
    try:
        response.raise_for_status()
    except HTTPError as error:
        if response.status_code == 429:
            raise ProviderRateLimitError("provider_rate_limited") from error
        raise
    return feedparser.parse(response.content)


def _match_rss_tickers(
    *,
    headline: str,
    summary: str | None,
    url: str,
    feed_spec: RssFeedSpec,
) -> list[str]:
    if not feed_spec.ticker_matches:
        return list(feed_spec.app_tickers)

    text = f"{headline}\n{summary or ''}\n{url}".lower()
    matched: list[str] = []
    for ticker_match in feed_spec.ticker_matches:
        if any(_contains_match_term(text, term) for term in ticker_match.match_terms):
            matched.append(ticker_match.ticker)
    return sorted({ticker.strip().upper() for ticker in matched if ticker.strip()})


def _contains_match_term(text: str, term: str) -> bool:
    return contains_term(text, term)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_utc(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return _to_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        try:
            return _to_utc(parsedate_to_datetime(raw))
        except (TypeError, ValueError):
            return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(value: datetime) -> str:
    return _to_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _http_get(
    url: str,
    *,
    params: dict[str, Any],
    timeout: int,
    headers: dict[str, str] | None = None,
):
    session = requests.Session()
    session.trust_env = False
    if headers:
        return session.get(url, params=params, timeout=timeout, headers=headers)
    return session.get(url, params=params, timeout=timeout)
