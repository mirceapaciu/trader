from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.product_components.market_data.models import MarketDataProvider
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.providers import (
    normalize_alpha_vantage_daily_bars,
    normalize_polygon_bars,
)


def test_normalize_alpha_vantage_daily_bars() -> None:
    symbol = default_provider_symbol(
        ticker="AXA",
        exchange_code="XPAR",
        provider=MarketDataProvider.ALPHA_VANTAGE,
    )
    fetched_at = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)

    bars = normalize_alpha_vantage_daily_bars(
        {
            "Time Series (Daily)": {
                "2026-06-14": {
                    "1. open": "10.0",
                    "2. high": "11.0",
                    "3. low": "9.5",
                    "4. close": "10.5",
                    "5. volume": "12345",
                }
            }
        },
        symbol=symbol,
        fetched_at=fetched_at,
    )

    assert len(bars) == 1
    assert bars[0].ticker == "AXA"
    assert bars[0].exchange_code == "XPAR"
    assert bars[0].provider == MarketDataProvider.ALPHA_VANTAGE
    assert bars[0].close_price == 10.5
    assert bars[0].volume == 12345


def test_normalize_polygon_bars_maps_utc_and_ohlcv() -> None:
    symbol = default_provider_symbol(
        ticker="AAPL",
        exchange_code="XNAS",
        provider=MarketDataProvider.POLYGON,
    )
    fetched_at = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)

    bars = normalize_polygon_bars(
        {
            "results": [
                # 2026-06-01T13:30:00Z == market open in ms epoch.
                {"t": 1780061400000, "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "v": 12345},
                {"t": 1780061460000, "o": 10.5, "h": 11.5, "l": 10.0, "c": 11.0, "v": 6789},
            ],
            "status": "OK",
        },
        symbol=symbol,
        interval="1m",
        fetched_at=fetched_at,
    )

    assert [bar.bar_start_at for bar in bars] == sorted(bar.bar_start_at for bar in bars)
    assert bars[0].provider == MarketDataProvider.POLYGON
    assert bars[0].bar_start_at.tzinfo == timezone.utc
    assert bars[0].ticker == "AAPL"
    assert bars[0].exchange_code == "XNAS"
    assert bars[0].open_price == 10.0
    assert bars[0].close_price == 10.5
    assert bars[0].volume == 12345
    assert bars[0].currency == "USD"


def test_normalize_polygon_bars_handles_empty_results() -> None:
    symbol = default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    assert normalize_polygon_bars({"status": "OK"}, symbol=symbol, interval="1m", fetched_at=datetime.now(timezone.utc)) == []


def test_polygon_symbol_supports_us_and_rejects_non_us() -> None:
    symbol = default_provider_symbol(
        ticker="aapl", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    assert symbol.provider_symbol == "AAPL"
    assert symbol.currency == "USD"

    with pytest.raises(ValueError):
        default_provider_symbol(
            ticker="RHM", exchange_code="XETR", provider=MarketDataProvider.POLYGON
        )
