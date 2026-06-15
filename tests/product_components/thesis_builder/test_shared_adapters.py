from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.shared.adapters import (
    PostgresSharedInstrumentRegistry,
    PostgresSharedThesisCardReviewWriter,
)


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sql: str | None = None
        self.params: tuple[Any, ...] | None = None
        self._rows = rows or []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


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
    assert "LEFT JOIN shared.t_instrument_aliases a" in cursor.sql
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
