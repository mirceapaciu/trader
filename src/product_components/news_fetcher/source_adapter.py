from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any
from urllib.parse import urlsplit

from src.core_components.event_ingestion_engine.interfaces import InboundSourceAdapter
from src.core_components.event_ingestion_engine.models import FetchedBatch, SourceEvent

from .providers import NewsProvider, ProviderArticle


@dataclass(frozen=True)
class SourceFilterConfig:
    """Relevance filter configuration applied before ingestion."""

    watchlist_tickers: set[str]
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]


class NewsSourceAdapter(InboundSourceAdapter):
    """Adapts provider payloads into the generic ingestion-engine source contract."""

    def __init__(
        self,
        *,
        provider: NewsProvider,
        timeout_seconds: int,
        filter_config: SourceFilterConfig,
    ) -> None:
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._filter = filter_config

    def fetch(self, source_key: str, cursor: Any | None) -> FetchedBatch:
        batch = self._provider.fetch(
            source_key=source_key,
            cursor=cursor,
            timeout_seconds=self._timeout_seconds,
        )
        events: list[SourceEvent] = []
        for article in batch.events:
            normalized = _normalize_article(article)
            if normalized is None:
                continue
            if not _is_relevant(normalized, self._filter):
                continue
            events.append(
                SourceEvent(
                    source=normalized.source,
                    source_event_id=normalized.provider_event_id,
                    canonical_locator=normalized.url,
                    title=normalized.headline,
                    summary=normalized.summary,
                    occurred_at=normalized.published_at,
                    entities=normalized.tickers,
                    attributes={
                        "tickers": normalized.tickers,
                        "sentiment_source": normalized.sentiment_source,
                        "fetched_at": normalized.fetched_at.isoformat(),
                    },
                )
            )
        return FetchedBatch(
            events=events,
            next_cursor=batch.next_cursor,
            cursor_updated_at=_to_utc(batch.cursor_updated_at),
        )


@dataclass(frozen=True)
class _NormalizedArticle:
    source: str
    provider_event_id: str | None
    headline: str
    summary: str | None
    url: str
    tickers: list[str]
    published_at: Any
    fetched_at: Any
    sentiment_source: float | None


def _normalize_article(article: ProviderArticle) -> _NormalizedArticle | None:
    source = article.source.strip().lower()
    headline = " ".join(article.headline.strip().split())
    if not source or not headline:
        return None

    url = article.url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    tickers = sorted({ticker.strip().upper() for ticker in article.tickers if ticker.strip()})
    summary = " ".join(article.summary.strip().split()) if article.summary else None

    return _NormalizedArticle(
        source=source,
        provider_event_id=article.provider_event_id,
        headline=headline,
        summary=summary,
        url=url,
        tickers=tickers,
        published_at=_to_utc(article.published_at),
        fetched_at=_to_utc(article.fetched_at),
        sentiment_source=article.sentiment_source,
    )


def _is_relevant(article: _NormalizedArticle, config: SourceFilterConfig) -> bool:
    text = f"{article.headline}\n{article.summary or ''}".lower()

    for keyword in config.exclude_keywords:
        if keyword in text:
            return False

    if config.watchlist_tickers and any(ticker in config.watchlist_tickers for ticker in article.tickers):
        return True

    return any(keyword in text for keyword in config.include_keywords)


def _to_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
