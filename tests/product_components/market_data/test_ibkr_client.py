from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.market_data.models import MarketDataProvider, QuoteDataType
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.providers import IbkrClient


class _FakeGateway:
    def __init__(self, *, connected: bool, raw: list[dict], quote: dict | None = None) -> None:
        self._connected = connected
        self._raw = raw
        self._quote = quote
        self.calls: list[dict] = []
        self.quote_calls: list[dict] = []

    def is_connected(self) -> bool:
        return self._connected

    def historical_bars(self, *, provider_symbol, interval, start, end, contract_metadata):
        self.calls.append(
            {
                "provider_symbol": provider_symbol,
                "interval": interval,
                "start": start,
                "end": end,
                "contract_metadata": contract_metadata,
            }
        )
        return self._raw

    def snapshot_quote(self, *, provider_symbol, contract_metadata=None, timeout_seconds=10.0):
        self.quote_calls.append(
            {
                "provider_symbol": provider_symbol,
                "contract_metadata": contract_metadata,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self._quote


def test_is_available_reflects_gateway_state() -> None:
    assert IbkrClient(gateway=None).is_available() is False
    assert IbkrClient(gateway=_FakeGateway(connected=False, raw=[])).is_available() is False
    assert IbkrClient(gateway=_FakeGateway(connected=True, raw=[])).is_available() is True


def test_fetch_historical_bars_returns_empty_without_gateway() -> None:
    symbol = default_provider_symbol(
        ticker="RHM", exchange_code="XETR", provider=MarketDataProvider.IBKR
    )
    client = IbkrClient(gateway=None)
    assert client.fetch_historical_bars(
        symbol,
        interval="1m",
        start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 2, tzinfo=timezone.utc),
    ) == []


def test_fetch_historical_bars_normalizes_gateway_bars() -> None:
    symbol = default_provider_symbol(
        ticker="RHM", exchange_code="XETR", provider=MarketDataProvider.IBKR
    )
    bar_start = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
    gateway = _FakeGateway(
        connected=True,
        raw=[{"bar_start_at": bar_start, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000}],
    )
    client = IbkrClient(gateway=gateway)

    bars = client.fetch_historical_bars(
        symbol,
        interval="1m",
        start=bar_start,
        end=bar_start,
    )

    assert len(bars) == 1
    assert bars[0].provider is MarketDataProvider.IBKR
    assert bars[0].ticker == "RHM"
    assert bars[0].currency == "EUR"
    assert bars[0].close_price == 10.5
    assert bars[0].bar_start_at == bar_start
    # The gateway is handed the provider symbol string and its IBKR contract metadata.
    assert gateway.calls[0]["provider_symbol"] == "RHM"
    assert gateway.calls[0]["contract_metadata"]["currency"] == "EUR"


def _quote_symbol():
    return default_provider_symbol(
        ticker="AAPL", exchange_code="XNAS", provider=MarketDataProvider.IBKR
    )


def _raw_quote(**overrides) -> dict:
    raw = {
        "bid": 209.5,
        "ask": 210.5,
        "last": 210.0,
        "close": 208.0,
        "volume": 12345.0,
        "timestamp": datetime(2026, 6, 16, 15, 0, tzinfo=timezone.utc),
        "market_data_type": 1,
    }
    raw.update(overrides)
    return raw


def test_fetch_quote_returns_none_without_gateway_or_connection() -> None:
    assert IbkrClient(gateway=None).fetch_quote(_quote_symbol()) is None

    gateway = _FakeGateway(connected=False, raw=[], quote=_raw_quote())
    assert IbkrClient(gateway=gateway).fetch_quote(_quote_symbol()) is None
    # A dead session must not even attempt the snapshot (would burn the timeout).
    assert gateway.quote_calls == []


def test_fetch_quote_maps_market_data_type_to_quote_data_type() -> None:
    expectations = {
        1: QuoteDataType.REALTIME,
        2: QuoteDataType.FROZEN,
        3: QuoteDataType.DELAYED,
        4: QuoteDataType.FROZEN,
    }
    for market_data_type, expected in expectations.items():
        gateway = _FakeGateway(
            connected=True, raw=[], quote=_raw_quote(market_data_type=market_data_type)
        )
        quote = IbkrClient(gateway=gateway).fetch_quote(_quote_symbol())
        assert quote is not None
        assert quote.data_type is expected, market_data_type


def test_fetch_quote_normalizes_fields_and_passes_timeout() -> None:
    gateway = _FakeGateway(connected=True, raw=[], quote=_raw_quote())
    client = IbkrClient(gateway=gateway, quote_timeout_seconds=5.0)

    quote = client.fetch_quote(_quote_symbol())

    assert quote is not None
    assert quote.provider is MarketDataProvider.IBKR
    assert quote.ticker == "AAPL"
    assert quote.bid_price == 209.5
    assert quote.ask_price == 210.5
    assert quote.last_price == 210.0
    assert quote.previous_close == 208.0
    assert quote.volume == 12345.0
    assert quote.provider_timestamp == datetime(2026, 6, 16, 15, 0, tzinfo=timezone.utc)
    assert quote.provider_metadata == {"market_data_type": 1}
    assert gateway.quote_calls[0]["timeout_seconds"] == 5.0
    assert gateway.quote_calls[0]["provider_symbol"] == "AAPL"


def test_fetch_quote_unknown_market_data_type_defaults_to_delayed() -> None:
    gateway = _FakeGateway(connected=True, raw=[], quote=_raw_quote(market_data_type=None))
    quote = IbkrClient(gateway=gateway).fetch_quote(_quote_symbol())
    assert quote is not None
    assert quote.data_type is QuoteDataType.DELAYED


def test_fetch_quote_returns_none_when_gateway_yields_nothing_or_no_prices() -> None:
    gateway = _FakeGateway(connected=True, raw=[], quote=None)
    assert IbkrClient(gateway=gateway).fetch_quote(_quote_symbol()) is None

    priceless = _raw_quote(bid=None, ask=None, last=None, close=None)
    gateway = _FakeGateway(connected=True, raw=[], quote=priceless)
    assert IbkrClient(gateway=gateway).fetch_quote(_quote_symbol()) is None
