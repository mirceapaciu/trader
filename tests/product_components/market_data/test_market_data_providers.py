from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.market_data.models import MarketDataProvider
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.providers import normalize_alpha_vantage_daily_bars


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
