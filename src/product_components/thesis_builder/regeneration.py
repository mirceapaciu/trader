from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from .llm_client import ThesisAnalyzer, TokenBudgetExhausted
from .models import NewsArticle
from .repository import PostgresThesisBuilderRepository
from .reprocessor import _row_to_article
from .service import (
    InstrumentRegistry,
    _json_ready,
    _resolve_instruments,
    _resolve_instrument_pairs,
    _triage_audit,
    _triage_rejection,
)

LOGGER = logging.getLogger("thesis_builder.regeneration")

# Emit an INFO heartbeat every N processed articles during a long regeneration run.
_HEARTBEAT_EVERY = 25

# (ticker, exchange_code, as_of) -> a market-context snapshot dataclass (or None).
# The snapshot is converted to the same JSON shape production uses before being fed
# to the analyzer, keeping analysis identical to live processing.
MarketContextProvider = Callable[[str, str, datetime], Any | None]

# (ticker, exchange_code, as_of) -> an InstrumentFundamentals dataclass (or None)
# holding the fundamentals visible at as_of, so regenerated prompts match what
# production saw at analysis time.
FundamentalsProvider = Callable[[str, str, datetime], Any | None]

# Reporting hook: (done, total, label) as articles are processed. The label is a
# short human hint (e.g. the current instrument), or None.
RegenerationProgress = Callable[[int, int, "str | None"], None]


@dataclass(frozen=True)
class RegenerationThresholds:
    required_evidence_count: int
    min_confidence: float
    min_relevance: float
    risk_max_loss_usd: float
    default_time_horizon: str
    evidence_collection_max_minutes: int
    max_evidence_age_minutes: int
    tradeability_max_entry_price: float = 1000.0
    tradeability_atr_stop_mult: float = 1.5
    triage_enabled: bool = False
    listicle_prefilter_enabled: bool = False
    listicle_prefilter_tag_threshold: int = 6
    synthesis_enabled: bool = False
    synthesis_model: str = "gpt-4o-mini"
    synthesis_max_output_tokens: int = 1200
    synthesis_fallback_to_mechanical: bool = False
    story_scoping_enabled: bool = False
    story_assignment_model: str = "gpt-4o-mini"
    story_assignment_max_output_tokens: int = 120
    already_priced_event_driven_atr_multiple: float = 1.5
    already_priced_event_driven_return_threshold: float = 0.04
    already_priced_sentiment_momentum_atr_multiple: float = 2.0
    already_priced_sentiment_momentum_return_threshold: float = 0.06


@dataclass(frozen=True)
class RegenerationResult:
    run_id: str
    articles_found: int
    articles_relevant: int
    articles_analyzed: int
    analyses_created: int
    cards_created: int
    budget_exhausted: bool
    llm_tokens_used: int = 0
    llm_cache_hits: int = 0
    llm_calls: int = 0
    # published_at of the last article analyzed before the token budget died; the
    # boundary up to which the window was actually covered. None when the run
    # covered the whole window (no exhaustion).
    analysis_coverage_until_at: datetime | None = None


class RegenerationRunner:
    """Re-runs the ThesisBuilder analysis workflow over a historical news window.

    This is the ThesisBuilder-owned regeneration entry point consumed by the
    Backtester. It reuses the exact production analysis (``ThesisAnalyzer``) and
    card-building logic
    (``PostgresThesisBuilderRepository.persist_analysis_and_update_evidence``) but:

    * reads articles by an explicit ``[window_start, window_end)`` publication window
      (not "days back from now"),
    * writes to an injected repository (an isolated per-run simulation schema), and
    * never touches Redis — no queue consumption and no signal publication.

    The result is that a regeneration backtest produces cards exactly as production
    would, without any impact on the live pipeline or data.
    """

    def __init__(
        self,
        *,
        dsn: str,
        news_fetcher_schema: str,
        run_id: str,
        repository: PostgresThesisBuilderRepository,
        analyzer: ThesisAnalyzer,
        instrument_registry: InstrumentRegistry,
        thresholds: RegenerationThresholds,
        market_context_provider: MarketContextProvider | None = None,
        fundamentals_provider: FundamentalsProvider | None = None,
        card_delay_seconds: int = 180,
        progress: RegenerationProgress | None = None,
    ) -> None:
        self._dsn = dsn
        self._news_fetcher_schema = news_fetcher_schema
        self._run_id = run_id
        self._repository = repository
        self._analyzer = analyzer
        self._instrument_registry = instrument_registry
        self._thresholds = thresholds
        self._market_context_provider = market_context_provider
        self._fundamentals_provider = fundamentals_provider
        self._card_delay_seconds = max(0, card_delay_seconds)
        self._progress = progress

    def run(self, *, window_start_at: datetime, window_end_at: datetime) -> RegenerationResult:
        articles = self._fetch_articles(
            window_start_at=window_start_at, window_end_at=window_end_at
        )
        articles_found = len(articles)
        active_instruments = self._instrument_registry.list_active_instruments()
        LOGGER.info(
            "Regeneration started run_id=%s articles=%d model=%s token_budget=%d",
            self._run_id,
            articles_found,
            self._analyzer.model,
            self._analyzer.max_tokens_per_run,
        )

        analyses_created = 0
        cards_created = 0
        articles_relevant = 0
        articles_analyzed = 0
        budget_exhausted = False
        # published_at of the last article that produced any analysis; on budget
        # exhaustion this becomes the coverage boundary reported to the caller.
        last_analyzed_published_at: datetime | None = None

        for index, article in enumerate(articles, start=1):
            if budget_exhausted:
                break
            # Analysis "occurs" at a feasible pipeline delay after publication; this
            # drives the card created_at (the engine's t_actual) and evidence-window
            # timing on the simulated clock.
            analysis_time = article.published_at + timedelta(seconds=self._card_delay_seconds)
            clock = lambda t=analysis_time: t
            article_analyzed = False

            if self._thresholds.listicle_prefilter_enabled:
                pair_resolution = _resolve_instrument_pairs(
                    article=article,
                    active_instruments=active_instruments,
                    listicle_prefilter_enabled=True,
                    listicle_prefilter_tag_threshold=self._thresholds.listicle_prefilter_tag_threshold,
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
                    article_analyzed = True
                instruments = pair_resolution.instruments
            else:
                instruments = _resolve_instruments(
                    article=article,
                    active_instruments=active_instruments,
                )
            if not instruments:
                if article_analyzed:
                    articles_relevant += 1
                    articles_analyzed += 1
                    last_analyzed_published_at = article.published_at
                self._report(index, articles_found, None)
                continue
            # This article matched at least one watchlisted instrument.
            articles_relevant += 1
            label = ",".join(sorted({i.ticker for i in instruments}))

            for instrument in instruments:
                context_snapshot = self._market_context(
                    ticker=instrument.ticker,
                    exchange_code=instrument.exchange_code,
                    as_of=analysis_time,
                )
                fundamentals_snapshot = self._fundamentals(
                    ticker=instrument.ticker,
                    exchange_code=instrument.exchange_code,
                    as_of=analysis_time,
                )
                if self._thresholds.triage_enabled:
                    try:
                        triage = self._analyzer.triage_article(
                            article=article,
                            ticker=instrument.ticker,
                            exchange_code=instrument.exchange_code,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Regeneration triage failed open run_id=%s article_id=%s ticker=%s",
                            self._run_id,
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
                            article_analyzed = True
                            continue
                try:
                    analysis = self._analyzer.analyze_article(
                        article=article,
                        ticker=instrument.ticker,
                        exchange_code=instrument.exchange_code,
                        market_context_snapshot=context_snapshot,
                        fundamentals_snapshot=fundamentals_snapshot,
                    )
                except TokenBudgetExhausted:
                    LOGGER.info(
                        "Regeneration token budget exhausted run_id=%s after %d analyses",
                        self._run_id,
                        analyses_created,
                    )
                    budget_exhausted = True
                    # If earlier instruments on this same article were analyzed, the
                    # article counts as covered; otherwise the boundary stays at the
                    # previous fully-analyzed article.
                    if article_analyzed:
                        last_analyzed_published_at = article.published_at
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
                    article_analyzed = True
                    continue
                except Exception:
                    LOGGER.exception(
                        "Regeneration LLM error run_id=%s article_id=%s ticker=%s",
                        self._run_id,
                        article.id,
                        instrument.ticker,
                    )
                    continue

                result = self._repository.persist_analysis_and_update_evidence(
                    article=article,
                    result=analysis,
                    market_context_snapshot=context_snapshot,
                    fundamentals_snapshot=fundamentals_snapshot,
                    instrument_display_name=getattr(instrument, "display_name", None),
                    instrument_aliases=getattr(instrument, "aliases", ()),
                    required_evidence_count=self._thresholds.required_evidence_count,
                    min_confidence=self._thresholds.min_confidence,
                    min_relevance=self._thresholds.min_relevance,
                    risk_max_loss_usd=self._thresholds.risk_max_loss_usd,
                    tradeability_max_entry_price=self._thresholds.tradeability_max_entry_price,
                    tradeability_atr_stop_mult=self._thresholds.tradeability_atr_stop_mult,
                    default_time_horizon=self._thresholds.default_time_horizon,
                    evidence_collection_max_minutes=self._thresholds.evidence_collection_max_minutes,
                    max_evidence_age_minutes=self._thresholds.max_evidence_age_minutes,
                    already_priced_event_driven_atr_multiple=self._thresholds.already_priced_event_driven_atr_multiple,
                    already_priced_event_driven_return_threshold=self._thresholds.already_priced_event_driven_return_threshold,
                    already_priced_sentiment_momentum_atr_multiple=self._thresholds.already_priced_sentiment_momentum_atr_multiple,
                    already_priced_sentiment_momentum_return_threshold=self._thresholds.already_priced_sentiment_momentum_return_threshold,
                    synthesis_enabled=self._thresholds.synthesis_enabled,
                    synthesis_model=self._thresholds.synthesis_model,
                    synthesis_max_output_tokens=self._thresholds.synthesis_max_output_tokens,
                    synthesis_fallback_to_mechanical=self._thresholds.synthesis_fallback_to_mechanical,
                    story_scoping_enabled=self._thresholds.story_scoping_enabled,
                    story_assignment_model=self._thresholds.story_assignment_model,
                    story_assignment_max_output_tokens=self._thresholds.story_assignment_max_output_tokens,
                    clock=clock,
                    reprocess_run_id=self._run_id,
                )
                analyses_created += 1
                article_analyzed = True
                if result.signal is not None:
                    cards_created += 1

            if article_analyzed:
                articles_analyzed += 1
                last_analyzed_published_at = article.published_at
            self._report(index, articles_found, label)
            # Heartbeat so a long run visibly progresses in the logs even when the
            # per-article DEBUG lines are suppressed.
            if index % _HEARTBEAT_EVERY == 0 or index == articles_found:
                LOGGER.info(
                    "Regeneration progress run_id=%s article=%d/%d analyses=%d cards=%d",
                    self._run_id,
                    index,
                    articles_found,
                    analyses_created,
                    cards_created,
                )

        LOGGER.info(
            "Regeneration done run_id=%s articles=%d relevant=%d analyzed=%d analyses=%d cards=%d budget_exhausted=%s",
            self._run_id,
            articles_found,
            articles_relevant,
            articles_analyzed,
            analyses_created,
            cards_created,
            budget_exhausted,
        )
        return RegenerationResult(
            run_id=self._run_id,
            articles_found=articles_found,
            articles_relevant=articles_relevant,
            articles_analyzed=articles_analyzed,
            analyses_created=analyses_created,
            cards_created=cards_created,
            budget_exhausted=budget_exhausted,
            analysis_coverage_until_at=(
                last_analyzed_published_at if budget_exhausted else None
            ),
        )

    def _market_context(
        self, *, ticker: str, exchange_code: str, as_of: datetime
    ) -> dict[str, Any] | None:
        if self._market_context_provider is None:
            return None
        snapshot = self._market_context_provider(ticker, exchange_code, as_of)
        if snapshot is None:
            return None
        # Match production: MarketData snapshot dataclass -> JSON-ready dict.
        return _json_ready(asdict(snapshot))

    def _fundamentals(
        self, *, ticker: str, exchange_code: str, as_of: datetime
    ) -> dict[str, Any] | None:
        if self._fundamentals_provider is None:
            return None
        snapshot = self._fundamentals_provider(ticker, exchange_code, as_of)
        if snapshot is None:
            return None
        # Match production (_load_fundamentals): dataclass -> JSON-ready dict, raw
        # provider payload dropped.
        snapshot_dict = _json_ready(asdict(snapshot))
        snapshot_dict.pop("payload", None)
        return snapshot_dict

    def _report(self, done: int, total: int, label: str | None) -> None:
        if self._progress is not None:
            self._progress(done, total, label)

    def _fetch_articles(
        self, *, window_start_at: datetime, window_end_at: datetime
    ) -> list[NewsArticle]:
        sql = (
            f"SELECT id, source, headline, summary, url, tickers, published_at, fetched_at, "
            f"sentiment_source "
            f"FROM {self._news_fetcher_schema}.t_news_articles "
            f"WHERE published_at >= %s AND published_at < %s "
            f"ORDER BY published_at, id"
        )
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (_to_utc(window_start_at), _to_utc(window_end_at)))
                rows = cur.fetchall()
        return [_row_to_article(row) for row in rows]


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
