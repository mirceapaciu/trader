from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from src.core_components.backtest_engine import Bar
from src.product_components.market_data.service import MarketDataService
from src.product_components.thesis_builder.export import ExportedThesisCard


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
