from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from src.product_components.market_data.context import build_market_context
from src.product_components.market_data.service import MarketDataService
from src.product_components.thesis_builder.export import ThesisCardHistoryExporter
from src.product_components.thesis_builder.llm_client import (
    OpenAIThesisClient,
    ThesisAnalyzer,
    ThesisLlmClient,
)
from src.product_components.thesis_builder.regeneration import (
    RegenerationResult,
    RegenerationRunner,
    RegenerationThresholds,
)
from src.product_components.thesis_builder.service import InstrumentRegistry
from src.product_components.thesis_builder.settings import ThesisBuilderSettings

from .clients import CardsProvider, RegenerationProgress

LOGGER = logging.getLogger("backtester.regeneration")

# Calendar days of daily bars pulled to reconstruct point-in-time market context.
# Enough to cover the longest lookback feature (50d SMA) with weekend/holiday slack.
_CONTEXT_LOOKBACK_DAYS = 90


class ThesisRegenerationProvider:
    """Backtester-side wiring that reuses ThesisBuilder to regenerate cards.

    Constructs the production analyzer + repository against an isolated simulation
    schema and reconstructs point-in-time market context from stored bars, so the
    regenerated analysis matches production exactly while writing only to the sim
    schema. Implements ``RegenerationProvider`` (see clients.py).
    """

    def __init__(
        self,
        *,
        dsn: str,
        thesis_settings: ThesisBuilderSettings,
        instrument_registry: InstrumentRegistry,
        market_data_service: MarketDataService,
        quote_max_age_seconds: int,
        llm_client_factory: Callable[[], ThesisLlmClient] | None = None,
    ) -> None:
        self._dsn = dsn
        self._thesis_settings = thesis_settings
        self._instrument_registry = instrument_registry
        self._market_data_service = market_data_service
        self._quote_max_age_seconds = quote_max_age_seconds
        # Overridable so tests can inject a deterministic client; production builds
        # the real OpenAI client from ThesisBuilder settings.
        self._llm_client_factory = llm_client_factory

    def thesis_config_snapshot(self, *, llm_model: str) -> dict:
        s = self._thesis_settings
        return {
            "llm_model": llm_model,
            "llm_max_output_tokens": s.llm_max_output_tokens,
            "required_evidence_count": s.required_evidence_count,
            "min_confidence": s.min_confidence,
            "min_relevance": s.min_relevance,
            "risk_max_loss_usd": s.risk_max_loss_usd,
            "default_time_horizon": s.default_time_horizon,
            "evidence_collection_max_minutes": s.evidence_collection_max_minutes,
            "max_evidence_age_minutes": s.max_evidence_age_minutes,
        }

    def regenerate(
        self,
        *,
        run_id: str,
        sim_schema: str,
        window_start_at: datetime,
        window_end_at: datetime,
        llm_model: str,
        token_budget: int,
        card_delay_seconds: int,
        progress: RegenerationProgress | None = None,
    ) -> RegenerationResult:
        # Late import to avoid a module import cycle (repository -> nothing heavy,
        # but keep the provider light for callers that never regenerate).
        from src.product_components.thesis_builder.repository import (
            PostgresThesisBuilderRepository,
        )

        s = self._thesis_settings
        repository = PostgresThesisBuilderRepository(
            dsn=self._dsn, thesis_schema=sim_schema
        )
        client = (
            self._llm_client_factory()
            if self._llm_client_factory is not None
            else OpenAIThesisClient(
                api_key=s.openai_api_key,
                request_timeout_seconds=s.llm_request_timeout_seconds,
                max_retries=s.llm_max_retries,
            )
        )
        analyzer = ThesisAnalyzer(
            client=client,
            model=llm_model,
            max_tokens_per_run=token_budget,
            max_tokens_per_item=s.llm_max_output_tokens,
        )
        thresholds = RegenerationThresholds(
            required_evidence_count=s.required_evidence_count,
            min_confidence=s.min_confidence,
            min_relevance=s.min_relevance,
            risk_max_loss_usd=s.risk_max_loss_usd,
            default_time_horizon=s.default_time_horizon,
            evidence_collection_max_minutes=s.evidence_collection_max_minutes,
            max_evidence_age_minutes=s.max_evidence_age_minutes,
        )
        runner = RegenerationRunner(
            dsn=self._dsn,
            news_fetcher_schema=s.news_fetcher_db_schema,
            run_id=run_id,
            repository=repository,
            analyzer=analyzer,
            instrument_registry=self._instrument_registry,
            thresholds=thresholds,
            market_context_provider=self._market_context,
            card_delay_seconds=card_delay_seconds,
            progress=progress,
        )
        return runner.run(
            window_start_at=window_start_at, window_end_at=window_end_at
        )

    def cards_provider(self, *, sim_schema: str) -> CardsProvider:
        return ThesisCardHistoryExporter(dsn=self._dsn, thesis_schema=sim_schema)

    def _market_context(self, ticker: str, exchange_code: str, as_of: datetime):
        """Reconstruct as-of market context from stored daily bars (no look-ahead).

        Uses only bars whose interval closed at or before ``as_of`` (get_historical_bars
        bounds by ``end``), and no live quote, so the context reflects only information
        available at analysis time.
        """
        start = as_of - timedelta(days=_CONTEXT_LOOKBACK_DAYS)
        bars = self._market_data_service.get_historical_bars(
            ticker=ticker,
            exchange_code=exchange_code,
            interval="1d",
            start=start,
            end=as_of,
        )
        if not bars:
            return None
        return build_market_context(
            ticker=ticker,
            exchange_code=exchange_code,
            quote=None,
            bars=bars,
            now=as_of,
            quote_max_age_seconds=self._quote_max_age_seconds,
        )
