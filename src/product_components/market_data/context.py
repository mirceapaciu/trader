from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import pstdev

from src.product_components.market_data.models import (
    ContextSourceStatus,
    MarketBar,
    MarketContextSnapshot,
    MarketQuote,
    QuoteDataType,
)


def classify_quote_status(
    quote: MarketQuote | None,
    *,
    now: datetime,
    max_age_seconds: int,
) -> ContextSourceStatus:
    if quote is None:
        return ContextSourceStatus.MISSING
    age = _to_utc(now) - _to_utc(quote.fetched_at)
    if age > timedelta(seconds=max_age_seconds):
        return ContextSourceStatus.STALE
    if quote.data_type is QuoteDataType.REALTIME:
        return ContextSourceStatus.FRESH
    if quote.data_type in {QuoteDataType.DELAYED, QuoteDataType.FROZEN}:
        return ContextSourceStatus.DELAYED
    if quote.data_type is QuoteDataType.STALE:
        return ContextSourceStatus.STALE
    return ContextSourceStatus.MISSING


def build_market_context(
    *,
    ticker: str,
    exchange_code: str,
    quote: MarketQuote | None,
    bars: list[MarketBar],
    now: datetime,
    quote_max_age_seconds: int,
) -> MarketContextSnapshot:
    ordered = sorted(bars, key=lambda bar: bar.bar_start_at)
    latest_bar = ordered[-1] if ordered else None
    quote_status = classify_quote_status(quote, now=now, max_age_seconds=quote_max_age_seconds)
    bars_fetched_at = max((_to_utc(bar.fetched_at) for bar in ordered), default=None)

    current_price = _first_not_none(
        quote.last_price if quote else None,
        latest_bar.close_price if latest_bar else None,
    )
    previous_close = _first_not_none(
        quote.previous_close if quote else None,
        ordered[-2].close_price if len(ordered) >= 2 else None,
    )

    source_status = quote_status
    if quote_status is ContextSourceStatus.MISSING and latest_bar is not None:
        source_status = ContextSourceStatus.STALE

    closes = [bar.close_price for bar in ordered]
    highs = [bar.high_price for bar in ordered]
    lows = [bar.low_price for bar in ordered]
    volumes = [bar.volume for bar in ordered if bar.volume is not None]

    recent_high = max(highs[-20:]) if len(highs) >= 1 else None
    recent_low = min(lows[-20:]) if len(lows) >= 1 else None

    return MarketContextSnapshot(
        ticker=ticker.strip().upper(),
        exchange_code=exchange_code.strip().upper(),
        as_of=_to_utc(now),
        source_status=source_status,
        current_price=current_price,
        previous_close=previous_close,
        return_1d=_return_from(closes, current_price, 1),
        return_5d=_return_from(closes, current_price, 5),
        return_20d=_return_from(closes, current_price, 20),
        atr_20d=_atr(ordered, 20),
        volatility_20d=_volatility(closes, 20),
        volume_ratio_20d=_volume_ratio(volumes, latest_bar.volume if latest_bar else None, 20),
        sma_20d=_sma(closes, 20),
        sma_50d=_sma(closes, 50),
        recent_high_20d=recent_high,
        recent_low_20d=recent_low,
        drawdown_from_high_20d=_drawdown(current_price, recent_high),
        quote_fetched_at=_to_utc(quote.fetched_at) if quote else None,
        bars_fetched_at=bars_fetched_at,
    )


def _return_from(closes: list[float], current_price: float | None, periods: int) -> float | None:
    if current_price is None or len(closes) < periods:
        return None
    base = closes[-periods]
    if base == 0:
        return None
    return (current_price - base) / base


def _sma(values: list[float], periods: int) -> float | None:
    if len(values) < periods:
        return None
    window = values[-periods:]
    return sum(window) / periods


def _atr(bars: list[MarketBar], periods: int) -> float | None:
    if len(bars) < periods + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(bars)):
        bar = bars[index]
        previous_close = bars[index - 1].close_price
        true_ranges.append(
            max(
                bar.high_price - bar.low_price,
                abs(bar.high_price - previous_close),
                abs(bar.low_price - previous_close),
            )
        )
    window = true_ranges[-periods:]
    return sum(window) / periods


def _volatility(closes: list[float], periods: int) -> float | None:
    if len(closes) < periods + 1:
        return None
    returns: list[float] = []
    for previous, current in zip(closes[-periods - 1 : -1], closes[-periods:]):
        if previous > 0:
            returns.append((current - previous) / previous)
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(252)


def _volume_ratio(volumes: list[float], latest_volume: float | None, periods: int) -> float | None:
    if latest_volume is None or len(volumes) < periods:
        return None
    average = sum(volumes[-periods:]) / periods
    if average == 0:
        return None
    return latest_volume / average


def _drawdown(current_price: float | None, recent_high: float | None) -> float | None:
    if current_price is None or recent_high is None or recent_high == 0:
        return None
    return (current_price - recent_high) / recent_high


def _first_not_none(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
