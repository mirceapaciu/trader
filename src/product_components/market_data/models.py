from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class MarketDataProvider(StrEnum):
    IBKR = "ibkr"
    ALPHA_VANTAGE = "alpha_vantage"


class QuoteDataType(StrEnum):
    REALTIME = "realtime"
    DELAYED = "delayed"
    FROZEN = "frozen"
    STALE = "stale"
    MISSING = "missing"


class ContextSourceStatus(StrEnum):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True)
class Instrument:
    ticker: str
    exchange_code: str


@dataclass(frozen=True)
class ProviderSymbol:
    ticker: str
    exchange_code: str
    provider: MarketDataProvider
    provider_symbol: str
    currency: str
    asset_class: str = "stock"
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    is_verified: bool = True
    is_enabled: bool = True


@dataclass(frozen=True)
class MarketQuote:
    ticker: str
    exchange_code: str
    provider: MarketDataProvider
    data_type: QuoteDataType
    currency: str
    bid_price: float | None
    ask_price: float | None
    last_price: float | None
    previous_close: float | None
    volume: float | None
    provider_timestamp: datetime | None
    fetched_at: datetime
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketBar:
    ticker: str
    exchange_code: str
    provider: MarketDataProvider
    bar_interval: str
    bar_start_at: datetime
    currency: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float | None
    adjusted: bool
    fetched_at: datetime
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketContextSnapshot:
    ticker: str
    exchange_code: str
    as_of: datetime
    source_status: ContextSourceStatus
    current_price: float | None
    previous_close: float | None
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    atr_20d: float | None
    volatility_20d: float | None
    volume_ratio_20d: float | None
    sma_20d: float | None
    sma_50d: float | None
    recent_high_20d: float | None
    recent_low_20d: float | None
    drawdown_from_high_20d: float | None
    quote_fetched_at: datetime | None
    bars_fetched_at: datetime | None


@dataclass(frozen=True)
class FetchRun:
    provider: MarketDataProvider
    operation: str
    ticker: str | None
    exchange_code: str | None
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime
    fetched_count: int
    details: dict[str, Any] = field(default_factory=dict)
