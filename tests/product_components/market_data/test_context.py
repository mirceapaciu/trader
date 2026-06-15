from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.market_data.context import build_market_context, classify_quote_status
from src.product_components.market_data.models import (
    ContextSourceStatus,
    MarketBar,
    MarketDataProvider,
    MarketQuote,
    QuoteDataType,
)


def _quote(*, fetched_at: datetime, data_type: QuoteDataType = QuoteDataType.REALTIME) -> MarketQuote:
    return MarketQuote(
        ticker="RHM",
        exchange_code="XETR",
        provider=MarketDataProvider.IBKR,
        data_type=data_type,
        currency="EUR",
        bid_price=100,
        ask_price=101,
        last_price=100,
        previous_close=99,
        volume=1000,
        provider_timestamp=fetched_at,
        fetched_at=fetched_at,
    )


def _bars(now: datetime, count: int = 60) -> list[MarketBar]:
    bars: list[MarketBar] = []
    for index in range(count):
        close = 100 + index
        bars.append(
            MarketBar(
                ticker="RHM",
                exchange_code="XETR",
                provider=MarketDataProvider.IBKR,
                bar_interval="1d",
                bar_start_at=now - timedelta(days=count - index),
                currency="EUR",
                open_price=close - 1,
                high_price=close + 2,
                low_price=close - 2,
                close_price=close,
                volume=1000 + index,
                adjusted=False,
                fetched_at=now,
            )
        )
    return bars


def test_classify_quote_status_realtime_delayed_stale_and_missing() -> None:
    now = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)

    assert classify_quote_status(_quote(fetched_at=now), now=now, max_age_seconds=1200) == ContextSourceStatus.FRESH
    assert (
        classify_quote_status(
            _quote(fetched_at=now, data_type=QuoteDataType.DELAYED),
            now=now,
            max_age_seconds=1200,
        )
        == ContextSourceStatus.DELAYED
    )
    assert (
        classify_quote_status(
            _quote(fetched_at=now - timedelta(seconds=1201)),
            now=now,
            max_age_seconds=1200,
        )
        == ContextSourceStatus.STALE
    )
    assert classify_quote_status(None, now=now, max_age_seconds=1200) == ContextSourceStatus.MISSING


def test_build_market_context_calculates_indicators() -> None:
    now = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)

    snapshot = build_market_context(
        ticker="RHM",
        exchange_code="XETR",
        quote=_quote(fetched_at=now),
        bars=_bars(now),
        now=now,
        quote_max_age_seconds=1200,
    )

    assert snapshot.source_status == ContextSourceStatus.FRESH
    assert snapshot.current_price == 100
    assert snapshot.previous_close == 99
    assert snapshot.sma_20d == sum(range(140, 160)) / 20
    assert snapshot.sma_50d == sum(range(110, 160)) / 50
    assert snapshot.atr_20d is not None
    assert snapshot.volatility_20d is not None
    assert snapshot.volume_ratio_20d is not None
    assert snapshot.recent_high_20d == 161
    assert snapshot.recent_low_20d == 138
    assert snapshot.drawdown_from_high_20d == (100 - 161) / 161
