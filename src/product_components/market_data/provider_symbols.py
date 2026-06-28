from __future__ import annotations

from src.product_components.market_data.models import MarketDataProvider, ProviderSymbol

_SUPPORTED_EXCHANGES = {"XNAS", "XNYS", "XETR", "XPAR"}


def normalize_exchange_code(exchange_code: str) -> str:
    normalized = exchange_code.strip().upper()
    aliases = {
        "NASDAQ": "XNAS",
        "NYSE": "XNYS",
        "ETR": "XETR",
        "EPA": "XPAR",
        "PAR": "XPAR",
    }
    return aliases.get(normalized, normalized)


def default_provider_symbol(
    *,
    ticker: str,
    exchange_code: str,
    provider: MarketDataProvider,
) -> ProviderSymbol:
    canonical_exchange = normalize_exchange_code(exchange_code)
    if canonical_exchange not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported_exchange_code:{canonical_exchange}")

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("missing_ticker")

    if provider is MarketDataProvider.IBKR:
        return _ibkr_symbol(normalized_ticker, canonical_exchange)
    if provider is MarketDataProvider.ALPHA_VANTAGE:
        return _alpha_vantage_symbol(normalized_ticker, canonical_exchange)
    if provider is MarketDataProvider.POLYGON:
        return _polygon_symbol(normalized_ticker, canonical_exchange)
    raise ValueError(f"unsupported_provider:{provider}")


def _ibkr_symbol(ticker: str, exchange_code: str) -> ProviderSymbol:
    exchange = {
        "XNAS": "SMART",
        "XNYS": "SMART",
        "XETR": "IBIS",
        "XPAR": "SBF",
    }[exchange_code]
    primary_exchange = {
        "XNAS": "NASDAQ",
        "XNYS": "NYSE",
        "XETR": "IBIS",
        "XPAR": "SBF",
    }[exchange_code]
    currency = {
        "XNAS": "USD",
        "XNYS": "USD",
        "XETR": "EUR",
        "XPAR": "EUR",
    }[exchange_code]
    return ProviderSymbol(
        ticker=ticker,
        exchange_code=exchange_code,
        provider=MarketDataProvider.IBKR,
        provider_symbol=ticker,
        currency=currency,
        provider_metadata={
            "exchange": exchange,
            "primary_exchange": primary_exchange,
            "sec_type": "STK",
        },
    )


_POLYGON_US_EXCHANGES = {"XNAS", "XNYS"}


def _polygon_symbol(ticker: str, exchange_code: str) -> ProviderSymbol:
    # Polygon's free tier covers US stocks only; non-US instruments route to IBKR instead.
    if exchange_code not in _POLYGON_US_EXCHANGES:
        raise ValueError(f"polygon_unsupported_exchange:{exchange_code}")
    return ProviderSymbol(
        ticker=ticker,
        exchange_code=exchange_code,
        provider=MarketDataProvider.POLYGON,
        provider_symbol=ticker,
        currency="USD",
        provider_metadata={},
    )


def _alpha_vantage_symbol(ticker: str, exchange_code: str) -> ProviderSymbol:
    suffix = {
        "XNAS": "",
        "XNYS": "",
        "XETR": ".DEX",
        "XPAR": ".PAR",
    }[exchange_code]
    currency = "USD" if exchange_code in {"XNAS", "XNYS"} else "EUR"
    return ProviderSymbol(
        ticker=ticker,
        exchange_code=exchange_code,
        provider=MarketDataProvider.ALPHA_VANTAGE,
        provider_symbol=f"{ticker}{suffix}",
        currency=currency,
        provider_metadata={"symbol_suffix": suffix},
    )
