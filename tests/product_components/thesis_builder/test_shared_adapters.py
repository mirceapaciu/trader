from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.shared.adapters import (
    PostgresSharedApiUsageWriter,
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    SharedLookupCacheEntry,
    PostgresSharedThesisCardReviewWriter,
    SharedInstrumentSeed,
    SharedWatchlistEntryInput,
)


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sql: str | None = None
        self.statements: list[str] = []
        self.params_history: list[tuple[Any, ...] | None] = []
        self.params: tuple[Any, ...] | None = None
        self._rows = rows or []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.statements.append(sql)
        self.params_history.append(params)
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> Any:
        # Return first configured row when available; fall back to a fake RETURNING id row.
        return self._rows[0] if self._rows else (1,)


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


def test_shared_instrument_registry_reads_shared_contract(monkeypatch) -> None:
    cursor = _FakeCursor(
        rows=[
            {
                "ticker": "AAPL",
                "exchange_code": "XNAS",
                "aliases": ["apple", "apple inc"],
            }
        ]
    )
    connection = _FakeConnection(cursor)
    registry = PostgresSharedInstrumentRegistry(
        dsn="unused",
        shared_schema="shared",
        watchlist_table="t_watchlist_tickers",
    )
    monkeypatch.setattr(registry, "_connect", lambda: connection)

    rows = registry.list_active_instruments()

    assert cursor.sql is not None
    assert "FROM shared.t_watchlist_tickers w" in cursor.sql
    assert "LEFT JOIN shared.t_exchange_listings el" in cursor.sql
    assert "LEFT JOIN shared.t_instruments i" in cursor.sql
    assert rows[0].ticker == "AAPL"
    assert rows[0].exchange_code == "XNAS"
    assert rows[0].aliases == ("apple", "apple inc")


def test_shared_review_writer_upserts_review_state(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    writer = PostgresSharedThesisCardReviewWriter(
        dsn="unused",
        shared_schema="shared",
    )
    monkeypatch.setattr(writer, "_connect", lambda: connection)

    writer.upsert_system_approved_review(
        card_id="card-1",
        reviewed_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )

    assert cursor.sql is not None
    assert "INSERT INTO shared.t_thesis_card_reviews" in cursor.sql
    assert "ON CONFLICT (card_id) DO UPDATE SET" in cursor.sql
    assert cursor.params is not None
    assert cursor.params[0] == "card-1"
    assert connection.committed is True


def test_shared_api_usage_writer_appends_usage(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    writer = PostgresSharedApiUsageWriter(
        dsn="unused",
        shared_schema="shared",
    )
    monkeypatch.setattr(writer, "_connect", lambda: connection)

    called_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
    writer.record_usage(
        provider="ibkr",
        endpoint="quote",
        called_at=called_at,
    )

    assert cursor.sql is not None
    assert "INSERT INTO shared.t_api_usage" in cursor.sql
    assert cursor.params == ("ibkr", "quote", None, None, called_at)
    assert connection.committed is True


def test_shared_instrument_admin_upserts_instruments_and_aliases(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    admin = PostgresSharedInstrumentAdmin(
        dsn="unused",
        shared_schema="shared",
    )
    monkeypatch.setattr(admin, "_connect", lambda: connection)

    admin.upsert_instruments(
        (
            SharedInstrumentSeed(
                ticker="RHM",
                exchange_code="ETR",
                display_name="Rheinmetall",
                aliases=("RHM", "Rheinmetall AG"),
                identifiers={"isin": "DE0007030009"},
                enabled=True,
            ),
        )
    )

    combined_sql = "\n".join(cursor.statements)
    assert "INSERT INTO shared.t_instruments" in combined_sql
    assert "INSERT INTO shared.t_exchange_listings" in combined_sql
    # ISIN is param[0], display_name param[1], aliases list param[2]
    assert any(params and params[0] == "DE0007030009" and params[1] == "Rheinmetall" for params in cursor.params_history)
    assert any(params and isinstance(params[2], list) and "Rheinmetall AG" in params[2] for params in cursor.params_history)
    assert connection.committed is True


def test_shared_instrument_registry_lists_watchlist_records(monkeypatch) -> None:
    cursor = _FakeCursor(
        rows=[
            {
                "ticker": "AAPL",
                "exchange_code": "XNAS",
                "display_name": "Apple Inc",
                "aliases": ["apple", "apple inc"],
                "is_active": True,
                "source": "manual",
            }
        ]
    )
    connection = _FakeConnection(cursor)
    registry = PostgresSharedInstrumentRegistry(
        dsn="unused",
        shared_schema="shared",
        watchlist_table="t_watchlist_tickers",
    )
    monkeypatch.setattr(registry, "_connect", lambda: connection)

    rows = registry.list_watchlist_records(active_only=True)

    assert rows[0].display_name == "Apple Inc"
    assert rows[0].aliases == ("apple", "apple inc")
    assert rows[0].has_missing_aliases is False


def test_shared_instrument_admin_upserts_watchlist_entry_and_cache(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    admin = PostgresSharedInstrumentAdmin(
        dsn="unused",
        shared_schema="shared",
    )
    monkeypatch.setattr(admin, "_connect", lambda: connection)

    admin.upsert_watchlist_entry(
        SharedWatchlistEntryInput(
            ticker="NVDA",
            exchange_code="XNAS",
            display_name="NVIDIA Corporation",
            aliases=("nvidia", "nvidia corporation"),
        )
    )
    admin.save_lookup_cache(
        operation="search",
        target="nvidia",
        provider="massive",
        payload={"results": [{"ticker": "NVDA"}]},
        fetched_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        expires_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )

    combined_sql = "\n".join(cursor.statements)
    assert "INSERT INTO shared.t_watchlist_tickers" in combined_sql
    assert "INSERT INTO shared.t_exchange_listings" in combined_sql
    assert "INSERT INTO shared.t_instrument_lookup_cache" in combined_sql
    assert connection.committed is True


def test_shared_instrument_admin_deactivates_watchlist_entry(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    admin = PostgresSharedInstrumentAdmin(
        dsn="unused",
        shared_schema="shared",
    )
    monkeypatch.setattr(admin, "_connect", lambda: connection)

    admin.deactivate_watchlist_entry(ticker="NVDA", exchange_code="XNAS")

    assert cursor.sql is not None
    assert "UPDATE shared.t_watchlist_tickers" in cursor.sql
    assert connection.committed is True
