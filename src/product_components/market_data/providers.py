from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import requests

from src.product_components.market_data.models import (
    MarketBar,
    MarketDataProvider,
    MarketQuote,
    ProviderSymbol,
    QuoteDataType,
)


class MarketDataProviderClient(Protocol):
    provider: MarketDataProvider

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        ...

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        ...

    def fetch_historical_bars(
        self,
        symbol: ProviderSymbol,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketBar]:
        ...


class AlphaVantageClient:
    provider = MarketDataProvider.ALPHA_VANTAGE

    def __init__(self, *, api_key: str, timeout_seconds: int = 10) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        return None

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        if not self._api_key.strip():
            return []
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol.provider_symbol,
                "outputsize": outputsize,
                "apikey": self._api_key,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return normalize_alpha_vantage_daily_bars(
            payload,
            symbol=symbol,
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_historical_bars(
        self,
        symbol: ProviderSymbol,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketBar]:
        # Alpha Vantage is daily-only backfill; intraday historical bars come from IBKR.
        return []


class IbkrClient:
    """Interactive Brokers market-data client.

    Quote and bar retrieval require a live IBKR Gateway/TWS session, so the fetch
    methods are best-effort and may be unavailable in offline environments.
    """

    provider = MarketDataProvider.IBKR

    def __init__(self, *, gateway: Any | None = None) -> None:
        self._gateway = gateway

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        return None

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        return self.fetch_historical_bars(
            symbol,
            interval="1d",
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
        )

    def fetch_historical_bars(
        self,
        symbol: ProviderSymbol,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketBar]:
        if self._gateway is None:
            return []
        raw_bars = self._gateway.historical_bars(
            provider_symbol=symbol.provider_symbol,
            interval=interval,
            start=start,
            end=end,
            contract_metadata=symbol.provider_metadata,
        )
        fetched_at = datetime.now(timezone.utc)
        return normalize_ibkr_historical_bars(
            raw_bars,
            symbol=symbol,
            interval=interval,
            fetched_at=fetched_at,
        )


def normalize_ibkr_historical_bars(
    raw_bars: list[dict[str, Any]],
    *,
    symbol: ProviderSymbol,
    interval: str,
    fetched_at: datetime,
) -> list[MarketBar]:
    bars: list[MarketBar] = []
    for raw_bar in raw_bars:
        if not isinstance(raw_bar, dict):
            continue
        bar_start = raw_bar["bar_start_at"]
        if not isinstance(bar_start, datetime):
            bar_start = datetime.fromisoformat(str(bar_start).replace("Z", "+00:00"))
        if bar_start.tzinfo is None:
            bar_start = bar_start.replace(tzinfo=timezone.utc)
        bars.append(
            MarketBar(
                ticker=symbol.ticker,
                exchange_code=symbol.exchange_code,
                provider=MarketDataProvider.IBKR,
                bar_interval=interval,
                bar_start_at=bar_start.astimezone(timezone.utc),
                currency=symbol.currency,
                open_price=float(raw_bar["open"]),
                high_price=float(raw_bar["high"]),
                low_price=float(raw_bar["low"]),
                close_price=float(raw_bar["close"]),
                volume=float(raw_bar["volume"]) if raw_bar.get("volume") is not None else None,
                adjusted=False,
                fetched_at=fetched_at,
                provider_metadata={"provider_symbol": symbol.provider_symbol},
            )
        )
    return sorted(bars, key=lambda bar: bar.bar_start_at)


def normalize_alpha_vantage_daily_bars(
    payload: dict[str, Any],
    *,
    symbol: ProviderSymbol,
    fetched_at: datetime,
) -> list[MarketBar]:
    series = payload.get("Time Series (Daily)")
    if not isinstance(series, dict):
        return []

    bars: list[MarketBar] = []
    for date_text, raw_bar in series.items():
        if not isinstance(raw_bar, dict):
            continue
        bars.append(
            MarketBar(
                ticker=symbol.ticker,
                exchange_code=symbol.exchange_code,
                provider=MarketDataProvider.ALPHA_VANTAGE,
                bar_interval="1d",
                bar_start_at=datetime.fromisoformat(str(date_text)).replace(tzinfo=timezone.utc),
                currency=symbol.currency,
                open_price=float(raw_bar["1. open"]),
                high_price=float(raw_bar["2. high"]),
                low_price=float(raw_bar["3. low"]),
                close_price=float(raw_bar["4. close"]),
                volume=float(raw_bar["5. volume"]),
                adjusted=False,
                fetched_at=fetched_at,
                provider_metadata={"provider_symbol": symbol.provider_symbol},
            )
        )
    return sorted(bars, key=lambda bar: bar.bar_start_at)


def normalize_ibkr_quote(
    *,
    symbol: ProviderSymbol,
    data_type: QuoteDataType,
    bid_price: float | None,
    ask_price: float | None,
    last_price: float | None,
    previous_close: float | None,
    volume: float | None,
    provider_timestamp: datetime | None,
    fetched_at: datetime,
    provider_metadata: dict[str, Any] | None = None,
) -> MarketQuote:
    return MarketQuote(
        ticker=symbol.ticker,
        exchange_code=symbol.exchange_code,
        provider=MarketDataProvider.IBKR,
        data_type=data_type,
        currency=symbol.currency,
        bid_price=bid_price,
        ask_price=ask_price,
        last_price=last_price,
        previous_close=previous_close,
        volume=volume,
        provider_timestamp=provider_timestamp,
        fetched_at=fetched_at,
        provider_metadata=provider_metadata or {},
    )
