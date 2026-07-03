from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.market_data.models import MarketDataProvider
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.providers import IbkrClient


class _FakeGateway:
    def __init__(self, *, connected: bool, raw: list[dict]) -> None:
        self._connected = connected
        self._raw = raw
        self.calls: list[dict] = []

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
