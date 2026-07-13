from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from src.product_components.market_data.models import (
    MarketBar,
    MarketDataProvider,
    MarketQuote,
    ProviderSymbol,
    QuoteDataType,
)
from src.product_components.market_data.settings import MarketDataSettings


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


_POLYGON_TIMESPAN: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
}


class PolygonClient:
    """Polygon.io aggregates client for historical OHLCV bars.

    Reuses the existing Massive/Polygon API key. A single aggregates request returns the
    full bar range for an instrument (up to 50k bars, paged via ``next_url``), which keeps
    a 50-instrument / 1-month backfill within the free-tier budget.
    """

    provider = MarketDataProvider.POLYGON

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.polygon.io",
        timeout_seconds: int = 30,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        """Previous-close 'quote' fallback for when IBKR is unavailable (free tier).

        Polygon's free tier has no live quote endpoint, so this serves the prior session's
        close via ``/v2/aggs/ticker/{sym}/prev``. Honesty rule: outside US regular trading
        hours the previous close *is* the market state (classified DELAYED, cards may
        validate); during RTH a day-old price is classified STALE so downstream gates
        reject rather than trade on it.
        """
        if not self._api_key.strip():
            return None
        response = requests.get(
            f"{self._base_url}/v2/aggs/ticker/{symbol.provider_symbol}/prev",
            params={"adjusted": "false", "apiKey": self._api_key},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        now = datetime.now(timezone.utc)
        return normalize_polygon_prev_close(
            response.json(),
            symbol=symbol,
            now=now,
            fetched_at=now,
        )

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        end = datetime.now(timezone.utc)
        # ``compact`` mirrors Alpha Vantage semantics (~100 sessions); ``full`` ~= 2 years.
        lookback_days = 100 if outputsize == "compact" else 730
        start = end - timedelta(days=lookback_days)
        return self.fetch_historical_bars(symbol, interval="1d", start=start, end=end)

    def fetch_historical_bars(
        self,
        symbol: ProviderSymbol,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketBar]:
        if not self._api_key.strip():
            return []
        timespan = _POLYGON_TIMESPAN.get(interval)
        if timespan is None:
            raise ValueError(f"unsupported_polygon_interval:{interval}")
        multiplier, unit = timespan
        from_ms = int(_to_utc(start).timestamp() * 1000)
        to_ms = int(_to_utc(end).timestamp() * 1000)
        url = (
            f"{self._base_url}/v2/aggs/ticker/{symbol.provider_symbol}/range/"
            f"{multiplier}/{unit}/{from_ms}/{to_ms}"
        )
        params: dict[str, Any] | None = {
            "adjusted": "false",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self._api_key,
        }
        fetched_at = datetime.now(timezone.utc)
        bars: list[MarketBar] = []
        # Follow ``next_url`` pagination; cap iterations to avoid runaway loops.
        for _ in range(100):
            response = requests.get(url, params=params, timeout=self._timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            bars.extend(
                normalize_polygon_bars(
                    payload,
                    symbol=symbol,
                    interval=interval,
                    fetched_at=fetched_at,
                )
            )
            next_url = payload.get("next_url")
            if not next_url:
                break
            url = next_url
            # ``next_url`` already carries the query string; only the key must be re-added.
            params = {"apiKey": self._api_key}
        return sorted(bars, key=lambda bar: bar.bar_start_at)


# IBKR ``marketDataType`` (from ``reqMarketDataType``) -> our freshness classification.
# 1=realtime, 2=frozen (last RTH values), 3=delayed (~15 min), 4=delayed-frozen.
_IBKR_MARKET_DATA_TYPE_TO_QUOTE_TYPE: dict[int, QuoteDataType] = {
    1: QuoteDataType.REALTIME,
    2: QuoteDataType.FROZEN,
    3: QuoteDataType.DELAYED,
    4: QuoteDataType.FROZEN,
}


class IbkrClient:
    """Interactive Brokers market-data client.

    Quote and bar retrieval require a live IBKR Gateway/TWS session, so the fetch
    methods are best-effort and may be unavailable in offline environments.
    """

    provider = MarketDataProvider.IBKR

    def __init__(self, *, gateway: Any | None = None, quote_timeout_seconds: float = 10.0) -> None:
        self._gateway = gateway
        self._quote_timeout_seconds = quote_timeout_seconds

    def is_available(self) -> bool:
        """True only when a gateway is injected and its IBKR session is live.

        The service uses this to prefer IBKR *only when actually connected* — otherwise it
        would mark ranges covered-but-empty and defeat the Polygon fallback.
        """
        return self._gateway is not None and bool(self._gateway.is_connected())

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        # A dead session would just burn the snapshot timeout; let the caller fall back.
        if not self.is_available():
            return None
        raw = self._gateway.snapshot_quote(
            provider_symbol=symbol.provider_symbol,
            contract_metadata=symbol.provider_metadata,
            timeout_seconds=self._quote_timeout_seconds,
        )
        if raw is None:
            return None
        has_price = any(raw.get(field) is not None for field in ("bid", "ask", "last", "close"))
        if not has_price:
            return None
        market_data_type = raw.get("market_data_type")
        try:
            type_key = int(market_data_type)
        except (TypeError, ValueError):
            type_key = -1
        # Unknown/unset marketDataType: prices exist, so assume delayed rather than drop.
        data_type = _IBKR_MARKET_DATA_TYPE_TO_QUOTE_TYPE.get(type_key, QuoteDataType.DELAYED)
        return normalize_ibkr_quote(
            symbol=symbol,
            data_type=data_type,
            bid_price=raw.get("bid"),
            ask_price=raw.get("ask"),
            last_price=raw.get("last"),
            previous_close=raw.get("close"),
            volume=raw.get("volume"),
            provider_timestamp=raw.get("timestamp"),
            fetched_at=datetime.now(timezone.utc),
            provider_metadata={"market_data_type": market_data_type},
        )

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


def build_provider_clients(
    settings: MarketDataSettings,
    *,
    ibkr_gateway: Any | None = None,
) -> dict[MarketDataProvider, MarketDataProviderClient]:
    """Construct the provider clients used for historical-bar backfills.

    Polygon is the primary free source for US instruments; IBKR is the non-US fallback and
    no-ops gracefully when no gateway is connected. Shared by the backtester CLI and the
    monitoring-UI in-process runner to keep wiring identical.
    """
    return {
        MarketDataProvider.POLYGON: PolygonClient(
            api_key=settings.polygon_api_key,
            base_url=settings.polygon_api_base_url,
        ),
        MarketDataProvider.IBKR: IbkrClient(
            gateway=ibkr_gateway,
            quote_timeout_seconds=settings.ibkr_quote_timeout_seconds,
        ),
        MarketDataProvider.ALPHA_VANTAGE: AlphaVantageClient(
            api_key=settings.alpha_vantage_api_key,
        ),
    }


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


_US_EASTERN = ZoneInfo("America/New_York")


def classify_polygon_prev_close_data_type(now: datetime) -> QuoteDataType:
    """Freshness of a previous-close price relative to US regular trading hours.

    Off-hours (nights, weekends) the previous close is the current market state, so it is
    DELAYED — good enough for downstream validity gates. During RTH (weekdays 09:30-16:00
    ET) a day-old price must not be traded on, so it is STALE. Known limitation: US market
    holidays falling on a weekday classify STALE during nominal RTH — conservative
    rejection rather than wrong acceptance.
    """
    local = _to_utc(now).astimezone(_US_EASTERN)
    if local.weekday() >= 5:
        return QuoteDataType.DELAYED
    minutes = local.hour * 60 + local.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return QuoteDataType.STALE
    return QuoteDataType.DELAYED


def normalize_polygon_prev_close(
    payload: dict[str, Any],
    *,
    symbol: ProviderSymbol,
    now: datetime,
    fetched_at: datetime,
) -> MarketQuote | None:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    raw = results[0]
    if not isinstance(raw, dict) or raw.get("c") is None:
        return None
    timestamp_ms = raw.get("t")
    volume = raw.get("v")
    return MarketQuote(
        ticker=symbol.ticker,
        exchange_code=symbol.exchange_code,
        provider=MarketDataProvider.POLYGON,
        data_type=classify_polygon_prev_close_data_type(now),
        currency=symbol.currency,
        bid_price=None,
        ask_price=None,
        last_price=float(raw["c"]),
        # Bars supply the true prior close in build_market_context; the prev-close bar's
        # own close IS the last price, not the close before it.
        previous_close=None,
        volume=float(volume) if volume is not None else None,
        provider_timestamp=(
            datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
            if timestamp_ms is not None
            else None
        ),
        fetched_at=fetched_at,
        provider_metadata={"source": "prev_close_aggregate"},
    )


def normalize_polygon_bars(
    payload: dict[str, Any],
    *,
    symbol: ProviderSymbol,
    interval: str,
    fetched_at: datetime,
) -> list[MarketBar]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    bars: list[MarketBar] = []
    for raw_bar in results:
        if not isinstance(raw_bar, dict) or "t" not in raw_bar:
            continue
        bar_start = datetime.fromtimestamp(int(raw_bar["t"]) / 1000, tz=timezone.utc)
        volume = raw_bar.get("v")
        bars.append(
            MarketBar(
                ticker=symbol.ticker,
                exchange_code=symbol.exchange_code,
                provider=MarketDataProvider.POLYGON,
                bar_interval=interval,
                bar_start_at=bar_start,
                currency=symbol.currency,
                open_price=float(raw_bar["o"]),
                high_price=float(raw_bar["h"]),
                low_price=float(raw_bar["l"]),
                close_price=float(raw_bar["c"]),
                volume=float(volume) if volume is not None else None,
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
