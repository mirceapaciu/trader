from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.product_components.market_data.models import MarketDataProvider, QuoteDataType
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.providers import (
    PolygonClient,
    classify_polygon_prev_close_data_type,
    normalize_alpha_vantage_daily_bars,
    normalize_polygon_bars,
    normalize_polygon_prev_close,
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


# 2026-06-16 is a Tuesday; times below are US Eastern (EDT = UTC-4).
_TUESDAY_NOON_ET = datetime(2026, 6, 16, 16, 0, tzinfo=timezone.utc)  # 12:00 ET, in RTH
_TUESDAY_EVENING_ET = datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)  # Tue 20:00 ET
_TUESDAY_PREOPEN_ET = datetime(2026, 6, 16, 13, 29, tzinfo=timezone.utc)  # 09:29 ET
_SATURDAY_ET = datetime(2026, 6, 20, 16, 0, tzinfo=timezone.utc)


def test_classify_polygon_prev_close_stale_during_rth() -> None:
    assert classify_polygon_prev_close_data_type(_TUESDAY_NOON_ET) is QuoteDataType.STALE


def test_classify_polygon_prev_close_delayed_off_hours() -> None:
    assert classify_polygon_prev_close_data_type(_TUESDAY_EVENING_ET) is QuoteDataType.DELAYED
    assert classify_polygon_prev_close_data_type(_TUESDAY_PREOPEN_ET) is QuoteDataType.DELAYED
    assert classify_polygon_prev_close_data_type(_SATURDAY_ET) is QuoteDataType.DELAYED


def test_normalize_polygon_prev_close_maps_quote_fields() -> None:
    symbol = default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    fetched_at = _TUESDAY_EVENING_ET

    quote = normalize_polygon_prev_close(
        {
            "results": [
                {"T": "AAPL", "c": 210.5, "o": 208.0, "h": 212.0, "l": 207.0, "v": 12345, "t": 1780061400000}
            ],
            "status": "OK",
        },
        symbol=symbol,
        now=_TUESDAY_EVENING_ET,
        fetched_at=fetched_at,
    )

    assert quote is not None
    assert quote.provider is MarketDataProvider.POLYGON
    assert quote.data_type is QuoteDataType.DELAYED
    assert quote.last_price == 210.5
    assert quote.previous_close is None  # bars supply the true prior close
    assert quote.volume == 12345
    assert quote.provider_timestamp == datetime.fromtimestamp(1780061400, tz=timezone.utc)
    assert quote.fetched_at == fetched_at
    assert quote.bid_price is None and quote.ask_price is None


def test_normalize_polygon_prev_close_handles_empty_or_priceless_results() -> None:
    symbol = default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    now = datetime.now(timezone.utc)
    assert normalize_polygon_prev_close({"status": "OK"}, symbol=symbol, now=now, fetched_at=now) is None
    assert normalize_polygon_prev_close({"results": []}, symbol=symbol, now=now, fetched_at=now) is None
    assert (
        normalize_polygon_prev_close({"results": [{"T": "AAPL"}]}, symbol=symbol, now=now, fetched_at=now)
        is None
    )


def test_polygon_fetch_quote_without_api_key_returns_none() -> None:
    symbol = default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    assert PolygonClient(api_key="").fetch_quote(symbol) is None


def test_polygon_fetch_quote_hits_prev_close_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.POLYGON
    )
    seen: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"results": [{"T": "AAPL", "c": 210.5, "v": 100, "t": 1780061400000}]}

    def _fake_get(url, *, params, timeout):
        seen["url"] = url
        seen["params"] = params
        return _Response()

    monkeypatch.setattr("src.product_components.market_data.providers.requests.get", _fake_get)

    quote = PolygonClient(api_key="test-key").fetch_quote(symbol)

    assert quote is not None
    assert quote.last_price == 210.5
    assert seen["url"].endswith("/v2/aggs/ticker/AAPL/prev")
    assert seen["params"]["apiKey"] == "test-key"


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
