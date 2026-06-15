from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.market_data.models import MarketDataProvider, ProviderSymbol
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter


class _FakeCursor:
    def __init__(self) -> None:
        self.sql: str | None = None
        self.params: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.sql = sql
        self.params = params


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self, *args, **kwargs) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_upsert_provider_symbol_uses_component_owned_schema(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    adapter = PostgresMarketDataStorageAdapter(
        dsn="unused",
        market_data_schema="market_data",
        shared_schema="shared",
        watchlist_table="t_watchlist_tickers",
    )
    monkeypatch.setattr(adapter, "_connect", lambda: connection)

    adapter.upsert_provider_symbol(
        ProviderSymbol(
            ticker="AXA",
            exchange_code="XPAR",
            provider=MarketDataProvider.ALPHA_VANTAGE,
            provider_symbol="AXA.PAR",
            currency="EUR",
            provider_metadata={"checked_at": datetime(2026, 6, 15, tzinfo=timezone.utc).isoformat()},
        )
    )

    assert cursor.sql is not None
    assert "INSERT INTO market_data.t_market_provider_symbols" in cursor.sql
    assert "ON CONFLICT (ticker, exchange_code, provider)" in cursor.sql
    assert cursor.params is not None
    assert cursor.params[:5] == ("AXA", "XPAR", "alpha_vantage", "AXA.PAR", "EUR")
    assert connection.committed is True
