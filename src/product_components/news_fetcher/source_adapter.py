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


@dataclass(frozen=True)
class NormalizedNewsArticle:
    """Normalized article fields needed by NewsFetcher filtering."""

    source: str
    provider_event_id: str | None
    headline: str
    summary: str | None
    url: str
    tickers: list[str]
    published_at: Any
    fetched_at: Any
    sentiment_source: float | None


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
            filter_outcome, rejection_reason_code = _evaluate_relevance(normalized, self._filter)
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
                        "pre_filter_outcome": filter_outcome,
                        "pre_filter_reason_code": rejection_reason_code,
                    },
                )
            )
        return FetchedBatch(
            events=events,
            next_cursor=batch.next_cursor,
            cursor_updated_at=_to_utc(batch.cursor_updated_at),
        )


def _normalize_article(article: ProviderArticle) -> NormalizedNewsArticle | None:
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

    return NormalizedNewsArticle(
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


def evaluate_relevance(
    article: NormalizedNewsArticle,
    config: SourceFilterConfig,
) -> tuple[str, str | None]:
    """Evaluate NewsFetcher relevance rules for one normalized article."""
    text = f"{article.headline}\n{article.summary or ''}".lower()

    for keyword in config.exclude_keywords:
        if keyword in text:
            return "rejected", "rejected_excluded_keyword"

    if config.watchlist_tickers and any(ticker in config.watchlist_tickers for ticker in article.tickers):
        return "accepted", None

    if any(keyword in text for keyword in config.include_keywords):
        return "accepted", None

    return "rejected", "rejected_not_relevant"


def _evaluate_relevance(article: NormalizedNewsArticle, config: SourceFilterConfig) -> tuple[str, str | None]:
    return evaluate_relevance(article, config)


def _to_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
