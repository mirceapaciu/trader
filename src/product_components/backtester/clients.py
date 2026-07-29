from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.core_components.backtest_engine import Bar
from src.product_components.market_data.service import MarketDataService
from src.product_components.thesis_builder.export import ExportedThesisCard

if TYPE_CHECKING:
    from src.product_components.thesis_builder.regeneration import RegenerationResult

WarmProgress = Callable[[int, int, str, str], None]


@runtime_checkable
class BarsProvider(Protocol):
    def historical_bars(
        self,
        *,
        ticker: str,
        exchange_code: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]: ...

    def warm(
        self,
        instruments: Iterable[tuple[str, str]],
        *,
        interval: str,
        start: datetime,
        end: datetime,
        progress: WarmProgress | None = None,
    ) -> None: ...


@runtime_checkable
class CardsProvider(Protocol):
    def export_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        validation_status: str | None = None,
        strategy: str | None = None,
    ) -> list[ExportedThesisCard]: ...


# Reporting hook for regeneration: (done, total, ticker).
RegenerationProgress = Callable[[int, int, str], None]


@runtime_checkable
class RegenerationProvider(Protocol):
    """Drives ThesisBuilder regeneration into an isolated simulation schema.

    Concrete implementations re-run the production analysis workflow (LLM analysis
    + card creation) over a historical window and write the resulting cards into
    ``sim_schema`` without touching production data or queues. The Backtester then
    reads those cards back through ``cards_provider`` and simulates them with the
    existing replay engine.
    """

    def thesis_config_snapshot(
        self,
        *,
        llm_model: str,
        required_evidence_count: int | None = None,
        evidence_collection_max_minutes: int | None = None,
        taxonomy_revision: int | None = None,
    ) -> dict: ...

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
        required_evidence_count: int | None = None,
        evidence_collection_max_minutes: int | None = None,
        taxonomy_revision: int | None = None,
        progress: RegenerationProgress | None = None,
    ) -> "RegenerationResult": ...

    def cards_provider(self, *, sim_schema: str) -> CardsProvider: ...


class MarketDataBarsProvider:
    """Adapt MarketDataService.get_historical_bars to the BarsProvider Protocol."""

    def __init__(self, *, market_data_service: MarketDataService) -> None:
        self._market_data_service = market_data_service

    def historical_bars(
        self,
        *,
        ticker: str,
        exchange_code: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        market_bars = self._market_data_service.get_historical_bars(
            ticker=ticker,
            exchange_code=exchange_code,
            interval=interval,
            start=start,
            end=end,
        )
        return [
            Bar(
                start_at=bar.bar_start_at,
                open=bar.open_price,
                high=bar.high_price,
                low=bar.low_price,
                close=bar.close_price,
                volume=bar.volume,
            )
            for bar in market_bars
        ]

    def warm(
        self,
        instruments: Iterable[tuple[str, str]],
        *,
        interval: str,
        start: datetime,
        end: datetime,
        progress: WarmProgress | None = None,
    ) -> None:
        self._market_data_service.prefetch_historical_bars(
            instruments,
            interval=interval,
            start=start,
            end=end,
            progress=progress,
        )
