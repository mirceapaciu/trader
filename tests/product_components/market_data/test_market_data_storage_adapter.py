from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from src.product_components.market_data.models import (
    InstrumentFundamentals,
    MarketDataProvider,
    ProviderSymbol,
)
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter
from src.product_components.shared.adapters import SharedInstrumentRecord


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


class _FakeInstrumentRegistry:
    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        return [SharedInstrumentRecord(ticker="AAPL", exchange_code="XNAS", aliases=("apple",))]


class _FakeApiUsageWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def record_usage(self, *, provider: str, endpoint: str, called_at: datetime, **kwargs) -> None:
        self.calls.append((provider, endpoint, called_at))


def test_upsert_provider_symbol_uses_component_owned_schema(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    adapter = PostgresMarketDataStorageAdapter(
        dsn="unused",
        market_data_schema="market_data",
        instrument_registry=_FakeInstrumentRegistry(),
        api_usage_writer=_FakeApiUsageWriter(),
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


def test_load_active_instruments_uses_shared_registry() -> None:
    adapter = PostgresMarketDataStorageAdapter(
        dsn="unused",
        market_data_schema="market_data",
        instrument_registry=_FakeInstrumentRegistry(),
        api_usage_writer=_FakeApiUsageWriter(),
    )

    rows = adapter.load_active_instruments()

    assert rows[0].ticker == "AAPL"
    assert rows[0].exchange_code == "XNAS"


def test_record_api_usage_uses_shared_usage_writer() -> None:
    usage_writer = _FakeApiUsageWriter()
    adapter = PostgresMarketDataStorageAdapter(
        dsn="unused",
        market_data_schema="market_data",
        instrument_registry=_FakeInstrumentRegistry(),
        api_usage_writer=usage_writer,
    )

    called_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
    adapter.record_api_usage(provider=MarketDataProvider.IBKR, endpoint="quote", called_at=called_at)

    assert usage_writer.calls == [("ibkr", "quote", called_at)]


def _adapter(monkeypatch, cursor: _FakeCursor, *, latest=None) -> PostgresMarketDataStorageAdapter:
    connection = _FakeConnection(cursor)
    adapter = PostgresMarketDataStorageAdapter(
        dsn="unused",
        market_data_schema="market_data",
        instrument_registry=_FakeInstrumentRegistry(),
        api_usage_writer=_FakeApiUsageWriter(),
    )
    monkeypatch.setattr(adapter, "_connect", lambda: connection)
    monkeypatch.setattr(
        adapter, "load_latest_fundamentals", lambda *, ticker, exchange_code: latest
    )
    return adapter


def _fundamentals(**overrides) -> InstrumentFundamentals:
    base = dict(
        ticker="AAPL",
        exchange_code="XNAS",
        market_cap_usd=3.0e12,
        shares_outstanding=1.5e10,
        revenue_ttm_usd=4.0e11,
        next_earnings_date=date(2026, 7, 30),
        provider=MarketDataProvider.FINNHUB,
        fetched_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        last_checked_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        payload={"profile2": {}},
    )
    base.update(overrides)
    return InstrumentFundamentals(**base)


def test_save_fundamentals_inserts_first_row(monkeypatch) -> None:
    cursor = _FakeCursor()
    adapter = _adapter(monkeypatch, cursor, latest=None)

    adapter.save_fundamentals(_fundamentals())

    assert "INSERT INTO market_data.t_instrument_fundamentals" in cursor.sql
    assert cursor.params[0] == "AAPL"
    assert cursor.params[3] == 3.0e12


def test_save_fundamentals_touches_last_checked_when_values_unchanged(monkeypatch) -> None:
    latest = _fundamentals()
    cursor = _FakeCursor()
    adapter = _adapter(monkeypatch, cursor, latest=latest)

    # Same prompt-visible values, later check time, different raw payload: the
    # existing point-in-time row is touched instead of split.
    adapter.save_fundamentals(
        _fundamentals(
            last_checked_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            payload={"profile2": {"noise": True}},
        )
    )

    assert "UPDATE market_data.t_instrument_fundamentals" in cursor.sql
    assert "SET last_checked_at" in cursor.sql
    # The touched row is addressed by the LATEST row's fetched_at.
    assert cursor.params == (
        datetime(2026, 7, 14, tzinfo=timezone.utc),
        "AAPL",
        "XNAS",
        latest.fetched_at,
    )


def test_save_fundamentals_inserts_new_row_when_values_changed(monkeypatch) -> None:
    cursor = _FakeCursor()
    adapter = _adapter(monkeypatch, cursor, latest=_fundamentals())

    adapter.save_fundamentals(_fundamentals(market_cap_usd=3.1e12))

    assert "INSERT INTO market_data.t_instrument_fundamentals" in cursor.sql
