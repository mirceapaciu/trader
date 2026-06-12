from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.news_fetcher.storage_adapter import PostgresNewsStorageAdapter


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

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_record_provider_cycle_status_qualifies_existing_last_non_zero_fetch_at(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    adapter = PostgresNewsStorageAdapter(
        dsn="unused",
        news_schema="news_fetcher",
        shared_schema="shared",
        watchlist_table="t_watchlist_tickers",
    )
    monkeypatch.setattr(adapter, "_connect", lambda: connection)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    adapter.record_provider_cycle_status(
        source_key="finnhub",
        started_at=now,
        finished_at=now,
        last_non_zero_fetch_at=None,
        status="success",
        error_code=None,
        fetched_count=0,
        accepted_count=0,
        rejected_count=0,
        checkpoint_advanced=False,
    )

    assert cursor.sql is not None
    assert (
        "COALESCE(EXCLUDED.last_non_zero_fetch_at, "
        "news_fetcher.t_provider_cycle_status.last_non_zero_fetch_at)"
    ) in cursor.sql
    assert connection.committed is True
