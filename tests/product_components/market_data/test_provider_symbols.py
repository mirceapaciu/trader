from __future__ import annotations

import pytest

from src.product_components.market_data.models import MarketDataProvider
from src.product_components.market_data.provider_symbols import (
    default_provider_symbol,
    normalize_exchange_code,
)


def test_normalize_exchange_aliases_to_mic_codes() -> None:
    assert normalize_exchange_code("ETR") == "XETR"
    assert normalize_exchange_code("NASDAQ") == "XNAS"
    assert normalize_exchange_code("PAR") == "XPAR"


@pytest.mark.parametrize(
    ("exchange_code", "currency", "ibkr_exchange", "alpha_symbol"),
    [
        ("XNAS", "USD", "SMART", "NVDA"),
        ("XNYS", "USD", "SMART", "IBM"),
        ("XETR", "EUR", "IBIS", "RHM.DEX"),
        ("XPAR", "EUR", "SBF", "AXA.PAR"),
    ],
)
def test_default_provider_symbols_cover_supported_markets(
    exchange_code: str,
    currency: str,
    ibkr_exchange: str,
    alpha_symbol: str,
) -> None:
    ticker = "NVDA" if exchange_code == "XNAS" else "IBM" if exchange_code == "XNYS" else "RHM" if exchange_code == "XETR" else "AXA"

    ibkr = default_provider_symbol(
        ticker=ticker,
        exchange_code=exchange_code,
        provider=MarketDataProvider.IBKR,
    )
    alpha = default_provider_symbol(
        ticker=ticker,
        exchange_code=exchange_code,
        provider=MarketDataProvider.ALPHA_VANTAGE,
    )

    assert ibkr.exchange_code == exchange_code
    assert ibkr.currency == currency
    assert ibkr.provider_metadata["exchange"] == ibkr_exchange
    assert alpha.provider_symbol == alpha_symbol


def test_default_provider_symbol_rejects_unsupported_exchange() -> None:
    with pytest.raises(ValueError, match="unsupported_exchange_code"):
        default_provider_symbol(
            ticker="SONY",
            exchange_code="XTKS",
            provider=MarketDataProvider.IBKR,
        )
