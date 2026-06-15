from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.product_components.news_fetcher.storage_adapter import PostgresNewsStorageAdapter
from src.product_components.shared.adapters import SharedInstrumentRecord


class _FakeCursor:
    def __init__(self, rows_by_query: list[list[dict[str, Any]]] | None = None) -> None:
        self.sql: str | None = None
        self.statements: list[str] = []
        self.params: tuple[Any, ...] | None = None
        self._rows_by_query = rows_by_query or []
        self._fetch_index = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.statements.append(sql)
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        if self._fetch_index >= len(self._rows_by_query):
            return []
        rows = self._rows_by_query[self._fetch_index]
        self._fetch_index += 1
        return rows


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
    def __init__(self, rows: list[SharedInstrumentRecord] | None = None) -> None:
        self.rows = rows or []

    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        return list(self.rows)


def test_record_provider_cycle_status_qualifies_existing_last_non_zero_fetch_at(monkeypatch) -> None:
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    adapter = PostgresNewsStorageAdapter(
        dsn="unused",
        news_schema="news_fetcher",
        instrument_registry=_FakeInstrumentRegistry(),
    )
    monkeypatch.setattr(adapter, "_connect", lambda: connection)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    adapter.record_provider_cycle_status(
        source_key="finnhub",
        started_at=now,
        finished_at=now,
        last_non_zero_fetch_at=None,
        last_fetch_attempt_at=None,
        status="success",
        error_code=None,
        fetched_count=0,
        accepted_count=0,
        rejected_count=0,
        checkpoint_advanced=False,
    )

    assert cursor.sql is not None
    assert (
        "COALESCE(EXCLUDED.last_fetch_attempt_at, "
        "news_fetcher.t_provider_cycle_status.last_fetch_attempt_at)"
    ) in cursor.sql
    assert (
        "COALESCE(EXCLUDED.last_non_zero_fetch_at, "
        "news_fetcher.t_provider_cycle_status.last_non_zero_fetch_at)"
    ) in cursor.sql
    assert connection.committed is True


def test_dynamic_rss_feed_specs_use_instrument_registry_and_news_tables(monkeypatch) -> None:
    cursor = _FakeCursor(
        rows_by_query=[
            [],
            [
                {
                    "source_key": "yahoo_finance",
                    "base_url": "https://feeds.finance.yahoo.com/rss/2.0/headline",
                    "symbol_param": "s",
                    "default_query_params": {"region": "US", "lang": "en-US"},
                    "max_symbols_per_request": 10,
                    "min_request_interval_seconds": 900,
                    "grouping_mode": "grouped",
                }
            ],
            [
                {
                    "source_key": "yahoo_finance",
                    "ticker": "RHM",
                    "exchange_code": "ETR",
                    "provider_symbol": "RHM.DE",
                    "query_params": {"region": "DE"},
                }
            ],
        ]
    )
    connection = _FakeConnection(cursor)
    adapter = PostgresNewsStorageAdapter(
        dsn="unused",
        news_schema="news_fetcher",
        instrument_registry=_FakeInstrumentRegistry(
            [
                SharedInstrumentRecord(ticker="RHM", exchange_code="ETR", aliases=("rheinmetall",)),
                SharedInstrumentRecord(ticker="AAPL", exchange_code="NASDAQ", aliases=("apple",)),
            ]
        ),
    )
    monkeypatch.setattr(adapter, "_connect", lambda: connection)

    specs = adapter.load_rss_feed_specs()

    assert len(specs) == 2
    rhm_spec = next(spec for spec in specs if spec.app_tickers == ("RHM",))
    aapl_spec = next(spec for spec in specs if spec.app_tickers == ("AAPL",))
    assert rhm_spec.provider_name == "yahoo_finance"
    assert "s=RHM.DE" in rhm_spec.url
    assert "region=DE" in rhm_spec.url
    assert rhm_spec.min_request_interval_seconds == 900
    assert rhm_spec.ticker_matches[0].ticker == "RHM"
    assert "rheinmetall" in rhm_spec.ticker_matches[0].match_terms
    assert "s=AAPL" in aapl_spec.url
    assert "region=US" in aapl_spec.url
    assert aapl_spec.ticker_matches[0].ticker == "AAPL"
    assert "apple" in aapl_spec.ticker_matches[0].match_terms
    combined_sql = "\n".join(cursor.statements)
    assert "news_fetcher.t_rss_sources" in combined_sql
    assert "news_fetcher.t_rss_symbol_rules" in combined_sql
    assert ".t_watchlist_tickers" not in combined_sql
    assert ".t_instrument_aliases" not in combined_sql
