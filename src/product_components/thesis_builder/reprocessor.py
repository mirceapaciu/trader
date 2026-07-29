from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .llm_client import ThesisAnalyzer, TokenBudgetExhausted
from .models import NewsArticle
from .repository import PostgresThesisBuilderRepository
from .service import (
    InstrumentRegistry,
    _resolve_instruments,
    _resolve_instrument_pairs,
    _triage_audit,
    _triage_rejection,
)

LOGGER = logging.getLogger("thesis_builder.reprocessor")


@dataclass(frozen=True)
class ReprocessResult:
    run_id: str
    articles_found: int
    analyses_created: int
    cards_created: int


class ThesisBuilderReprocessor:
    def __init__(
        self,
        *,
        dsn: str,
        news_fetcher_schema: str,
        thesis_schema: str,
        repository: PostgresThesisBuilderRepository,
        analyzer: ThesisAnalyzer,
        instrument_registry: InstrumentRegistry,
        required_evidence_count: int,
        min_confidence: float,
        min_relevance: float = 0.0,
        risk_max_loss_usd: float,
        default_time_horizon: str,
        evidence_collection_max_minutes: int,
        max_evidence_age_minutes: int,
        tradeability_max_entry_price: float = 1000.0,
        tradeability_atr_stop_mult: float = 1.5,
        triage_enabled: bool = False,
        listicle_prefilter_enabled: bool = False,
        listicle_prefilter_tag_threshold: int = 6,
        already_priced_event_driven_atr_multiple: float = 1.5,
        already_priced_event_driven_return_threshold: float = 0.04,
        already_priced_sentiment_momentum_atr_multiple: float = 2.0,
        already_priced_sentiment_momentum_return_threshold: float = 0.06,
        story_scoping_enabled: bool = False,
        story_assignment_model: str | None = None,
        story_assignment_max_output_tokens: int | None = None,
        taxonomy_revision: int = 1,
    ) -> None:
        self._dsn = dsn
        self._news_fetcher_schema = news_fetcher_schema
        self._thesis_schema = thesis_schema
        self._repository = repository
        self._analyzer = analyzer
        self._instrument_registry = instrument_registry
        self._required_evidence_count = required_evidence_count
        self._min_confidence = min_confidence
        self._min_relevance = min_relevance
        self._risk_max_loss_usd = risk_max_loss_usd
        self._tradeability_max_entry_price = tradeability_max_entry_price
        self._tradeability_atr_stop_mult = tradeability_atr_stop_mult
        self._default_time_horizon = default_time_horizon
        self._evidence_collection_max_minutes = evidence_collection_max_minutes
        self._max_evidence_age_minutes = max_evidence_age_minutes
        self._triage_enabled = triage_enabled
        self._listicle_prefilter_enabled = listicle_prefilter_enabled
        self._listicle_prefilter_tag_threshold = listicle_prefilter_tag_threshold
        self._already_priced_event_driven_atr_multiple = already_priced_event_driven_atr_multiple
        self._already_priced_event_driven_return_threshold = already_priced_event_driven_return_threshold
        self._already_priced_sentiment_momentum_atr_multiple = already_priced_sentiment_momentum_atr_multiple
        self._already_priced_sentiment_momentum_return_threshold = already_priced_sentiment_momentum_return_threshold
        self._story_scoping_enabled = story_scoping_enabled
        self._story_assignment_model = story_assignment_model
        self._story_assignment_max_output_tokens = story_assignment_max_output_tokens
        self._taxonomy_revision = taxonomy_revision

    def reprocess(self, *, days_back: int, max_articles: int = 200) -> ReprocessResult:
        run_id = str(uuid.uuid4())
        articles = self._fetch_articles(days_back=days_back)
        articles = sorted(articles, key=lambda a: a.published_at)
        articles_found = len(articles)
        if len(articles) > max_articles:
            # Keep the most recent articles (the tail of the ascending sort) so a
            # large backlog does not cause us to only reprocess the oldest, least
            # relevant news. Processing stays chronological for evidence windows.
            articles = articles[-max_articles:]
        LOGGER.info(
            "Reprocessing started run_id=%s articles_found=%d processing=%d days_back=%d token_budget=%d",
            run_id, articles_found, len(articles), days_back, self._analyzer.max_tokens_per_run,
        )

        active_instruments = self._instrument_registry.list_active_instruments()
        analyses_created = 0
        cards_created = 0
        budget_exhausted = False

        for article_idx, article in enumerate(articles):
            if budget_exhausted:
                break

            published_at = article.published_at
            def clock(published_at=published_at):
                return published_at + timedelta(minutes=5)

            if self._listicle_prefilter_enabled:
                pair_resolution = _resolve_instrument_pairs(
                    article=article,
                    active_instruments=active_instruments,
                    listicle_prefilter_enabled=True,
                    listicle_prefilter_tag_threshold=self._listicle_prefilter_tag_threshold,
                )
                for instrument in pair_resolution.prefiltered_roundup:
                    self._repository.persist_rejected_analysis(
                        article=article,
                        instrument=instrument,
                        rejection_reason_code="prefiltered_roundup",
                        llm_model="deterministic_prefilter",
                        validation_errors=["prefiltered_roundup"],
                    )
                    analyses_created += 1
                instruments = pair_resolution.instruments
            else:
                instruments = _resolve_instruments(
                    article=article,
                    active_instruments=active_instruments,
                )
            if not instruments:
                continue

            LOGGER.debug(
                "Reprocessing article run_id=%s idx=%d/%d article_id=%s instruments=%s",
                run_id, article_idx + 1, len(articles), article.id,
                [i.ticker for i in instruments],
            )

            for instrument in instruments:
                if self._triage_enabled:
                    try:
                        triage = self._analyzer.triage_article(
                            article=article,
                            ticker=instrument.ticker,
                            exchange_code=instrument.exchange_code,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Reprocess triage failed open run_id=%s article_id=%s ticker=%s",
                            run_id,
                            article.id,
                            instrument.ticker,
                        )
                    else:
                        triage_rejection = _triage_rejection(triage)
                        if triage_rejection is not None:
                            self._repository.persist_rejected_analysis(
                                article=article,
                                instrument=instrument,
                                rejection_reason_code=triage_rejection,
                                llm_model=triage.llm_model,
                                validation_errors=[triage_rejection, _triage_audit(triage)],
                                triage_result=triage,
                            )
                            analyses_created += 1
                            continue
                try:
                    analysis = self._analyzer.analyze_article(
                        article=article,
                        ticker=instrument.ticker,
                        exchange_code=instrument.exchange_code,
                        market_context_snapshot=None,
                        taxonomy_revision=self._taxonomy_revision,
                    )
                except TokenBudgetExhausted:
                    LOGGER.info(
                        "Reprocessing token budget exhausted run_id=%s after %d analyses — stopping early",
                        run_id, analyses_created,
                    )
                    budget_exhausted = True
                    break
                except ValueError as exc:
                    self._repository.persist_rejected_analysis(
                        article=article,
                        instrument=instrument,
                        rejection_reason_code=str(exc) or "invalid_llm_response",
                        llm_model=self._analyzer.model,
                        validation_errors=[str(exc) or "invalid_llm_response"],
                    )
                    analyses_created += 1
                    continue
                except Exception:
                    LOGGER.exception(
                        "Reprocess LLM error run_id=%s article_id=%s ticker=%s",
                        run_id, article.id, instrument.ticker,
                    )
                    continue

                LOGGER.info(
                    "Analysis run_id=%s ticker=%s strategy=%s direction=%s confidence=%.2f",
                    run_id, instrument.ticker,
                    analysis.candidate_strategy.value, analysis.direction.value, analysis.confidence,
                )
                result = self._repository.persist_analysis_and_update_evidence(
                    article=article,
                    result=analysis,
                    market_context_snapshot=None,
                    instrument_display_name=getattr(instrument, "display_name", None),
                    instrument_aliases=getattr(instrument, "aliases", ()),
                    required_evidence_count=self._required_evidence_count,
                    min_confidence=self._min_confidence,
                    min_relevance=self._min_relevance,
                    risk_max_loss_usd=self._risk_max_loss_usd,
                    tradeability_max_entry_price=self._tradeability_max_entry_price,
                    tradeability_atr_stop_mult=self._tradeability_atr_stop_mult,
                    default_time_horizon=self._default_time_horizon,
                    evidence_collection_max_minutes=self._evidence_collection_max_minutes,
                    max_evidence_age_minutes=self._max_evidence_age_minutes,
                    already_priced_event_driven_atr_multiple=self._already_priced_event_driven_atr_multiple,
                    already_priced_event_driven_return_threshold=self._already_priced_event_driven_return_threshold,
                    already_priced_sentiment_momentum_atr_multiple=self._already_priced_sentiment_momentum_atr_multiple,
                    already_priced_sentiment_momentum_return_threshold=self._already_priced_sentiment_momentum_return_threshold,
                    story_scoping_enabled=self._story_scoping_enabled,
                    story_assignment_model=self._story_assignment_model,
                    story_assignment_max_output_tokens=self._story_assignment_max_output_tokens,
                    clock=clock,
                    reprocess_run_id=run_id,
                )
                analyses_created += 1
                if result.signal is not None:
                    LOGGER.info(
                        "Reprocessing card created run_id=%s ticker=%s article_id=%s",
                        run_id, instrument.ticker, article.id,
                    )
                    cards_created += 1

        LOGGER.info(
            "Reprocessing done run_id=%s articles_found=%d processed=%d analyses_created=%d cards_created=%d budget_exhausted=%s",
            run_id, articles_found, len(articles), analyses_created, cards_created, budget_exhausted,
        )
        return ReprocessResult(
            run_id=run_id,
            articles_found=articles_found,
            analyses_created=analyses_created,
            cards_created=cards_created,
        )

    def _fetch_articles(self, *, days_back: int) -> list[NewsArticle]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        sql = (
            f"SELECT id, source, headline, summary, url, tickers, published_at, fetched_at, sentiment_source "
            f"FROM {self._news_fetcher_schema}.t_news_articles "
            f"WHERE published_at >= %s "
            f"ORDER BY published_at"
        )
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (cutoff,))
                rows = cur.fetchall()
        return [_row_to_article(row) for row in rows]


def _row_to_article(row: dict[str, Any]) -> NewsArticle:
    published_at = row["published_at"]
    fetched_at = row["fetched_at"]
    if not isinstance(published_at, datetime):
        published_at = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    if not isinstance(fetched_at, datetime):
        fetched_at = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    tickers = row.get("tickers") or []
    return NewsArticle(
        id=str(row["id"]),
        source=str(row["source"]),
        headline=str(row["headline"]),
        summary=row.get("summary"),
        url=str(row["url"]),
        tickers=[str(t).strip().upper() for t in tickers if str(t).strip()],
        published_at=published_at,
        fetched_at=fetched_at,
        sentiment_source=row.get("sentiment_source"),
    )
