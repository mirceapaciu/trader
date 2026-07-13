from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from src.core_components.event_ingestion_engine.deduplication import evaluate_dedupe
from src.core_components.event_ingestion_engine.models import (
    FilterOutcome,
    FilterResult,
    FilterRun,
    SoftDedupePolicy,
)
from src.product_components.filter_quality_evaluator.models import InputArticle
from src.product_components.news_fetcher.source_adapter import (
    NormalizedNewsArticle,
    SourceFilterConfig,
    evaluate_relevance,
)

from .filter_config import NewsFilterConfig
from .settings import NewsFetcherSettings


@dataclass(frozen=True)
class ReprocessArticleCandidate:
    article: InputArticle
    source_key: str
    already_accepted: bool = False
    already_published: bool = False


@dataclass(frozen=True)
class ReprocessRejectedResult:
    scanned_rejected_count: int
    newly_accepted_count: int
    still_rejected_count: int
    already_accepted_count: int
    already_published_count: int
    queued_publication_obligation_count: int


class ReprocessRejectedStorage(Protocol):
    def load_active_watchlist_tickers(self) -> set[str]: ...

    def seed_production_filter_config_if_missing(
        self,
        *,
        include_keywords: tuple[str, ...],
        exclude_keywords: tuple[str, ...],
        watchlist_tickers: set[str],
        dedupe_algorithm: str,
        dedupe_similarity_threshold: float,
        dedupe_lookback_hours: int,
    ) -> NewsFilterConfig: ...

    def load_reprocess_rejected_articles(
        self,
        *,
        fetched_since: datetime,
        fetched_until: datetime,
    ) -> list[ReprocessArticleCandidate]: ...

    def load_reprocess_dedupe_context_articles(
        self,
        *,
        published_since: datetime,
        published_until: datetime,
    ) -> list[InputArticle]: ...

    def persist_reprocess_rejected_results(
        self,
        *,
        filter_run: FilterRun,
        candidates: list[ReprocessArticleCandidate],
        results: list[FilterResult],
    ) -> int: ...


class NewsFetcherRejectedArticleReprocessor:
    def __init__(
        self,
        *,
        settings: NewsFetcherSettings,
        storage: ReprocessRejectedStorage,
        now_factory=None,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def reprocess_window(self, *, fetched_since: datetime, fetched_until: datetime) -> ReprocessRejectedResult:
        window_start = _to_utc(fetched_since)
        window_end = _to_utc(fetched_until)
        if window_start >= window_end:
            raise ValueError("invalid_reprocess_window")

        filter_config = self._effective_production_filter_config()
        candidates = self._storage.load_reprocess_rejected_articles(
            fetched_since=window_start,
            fetched_until=window_end,
        )
        actionable_candidates = [
            candidate
            for candidate in candidates
            if not candidate.already_accepted and not candidate.already_published
        ]
        context = self._storage.load_reprocess_dedupe_context_articles(
            published_since=window_start - timedelta(hours=filter_config.dedupe_lookback_hours),
            published_until=window_end,
        )
        target_results = _evaluate_reprocess_candidates(
            context=context,
            candidates=actionable_candidates,
            filter_config=filter_config,
        )
        actionable_by_id = {candidate.article.id: candidate for candidate in actionable_candidates}
        newly_accepted = [
            result
            for result in target_results
            if result.outcome == FilterOutcome.ACCEPTED
        ]
        queued = self._storage.persist_reprocess_rejected_results(
            filter_run=filter_config.production_filter_run(),
            candidates=list(actionable_by_id.values()),
            results=target_results,
        )
        return ReprocessRejectedResult(
            scanned_rejected_count=len(candidates),
            newly_accepted_count=len(newly_accepted),
            still_rejected_count=len(target_results) - len(newly_accepted),
            already_accepted_count=sum(1 for candidate in candidates if candidate.already_accepted),
            already_published_count=sum(1 for candidate in candidates if candidate.already_published),
            queued_publication_obligation_count=queued,
        )

    def _effective_production_filter_config(self) -> NewsFilterConfig:
        watchlist = self._storage.load_active_watchlist_tickers()
        config = self._storage.seed_production_filter_config_if_missing(
            include_keywords=self._settings.include_keywords,
            exclude_keywords=self._settings.exclude_keywords,
            watchlist_tickers=watchlist,
            dedupe_algorithm=self._settings.dedupe_algorithm,
            dedupe_similarity_threshold=self._settings.dedupe_similarity_threshold,
            dedupe_lookback_hours=self._settings.dedupe_lookback_hours,
        )
        return replace(config, watchlist_tickers=tuple(sorted(watchlist)))


def _evaluate_reprocess_candidates(
    *,
    context: list[InputArticle],
    candidates: list[ReprocessArticleCandidate],
    filter_config: NewsFilterConfig,
) -> list[FilterResult]:
    accepted_events = [
        article.canonical_event()
        for article in sorted(context, key=lambda item: (item.published_at, item.id))
    ]
    source_filter = SourceFilterConfig(
        watchlist_tickers=set(filter_config.watchlist_tickers),
        include_keywords=filter_config.include_keywords,
        exclude_keywords=filter_config.exclude_keywords,
    )
    dedupe_policy = SoftDedupePolicy(
        enabled=True,
        algorithm=filter_config.dedupe_algorithm,
        threshold=filter_config.dedupe_similarity_threshold,
        lookback_window=timedelta(hours=filter_config.dedupe_lookback_hours),
        max_time_delta_hours=filter_config.dedupe_lookback_hours,
    )

    results: list[FilterResult] = []
    for candidate in sorted(candidates, key=lambda item: (item.article.published_at, item.article.id)):
        article = candidate.article
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
        outcome, reason = evaluate_relevance(normalized, source_filter)
        if outcome == FilterOutcome.REJECTED.value:
            results.append(
                FilterResult(
                    article_id=article.id,
                    outcome=FilterOutcome.REJECTED,
                    rejection_reason_code=reason,
                )
            )
            continue

        event = article.canonical_event()
        decision = evaluate_dedupe(event, accepted_events, dedupe_policy)
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


def build_reprocess_envelope(*, article: InputArticle, now: datetime | None = None) -> dict:
    published_at = _to_utc(now or datetime.now(timezone.utc))
    occurred_at = _to_utc(article.published_at)
    fetched_at = _to_utc(article.fetched_at)
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": "news.article.created",
        "event_version": "1.0",
        "occurred_at": _format_seconds(occurred_at),
        "published_at": _format_seconds(published_at),
        "producer": "news_fetcher",
        "dedupe_key": article.id,
        "payload": {
            "id": article.id,
            "source": article.source,
            "source_event_id": article.id,
            "canonical_locator": article.url,
            "title": article.headline,
            "summary": article.summary,
            "occurred_at": _format_seconds(occurred_at),
            "ingested_at": _format_seconds(fetched_at),
            "payload_version": "1.0",
            "entities": article.tickers,
            "attributes": {
                "tickers": article.tickers,
                "sentiment_source": article.sentiment_source,
                "fetched_at": fetched_at.isoformat(),
            },
        },
    }


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_seconds(value: datetime) -> str:
    return _to_utc(value).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
