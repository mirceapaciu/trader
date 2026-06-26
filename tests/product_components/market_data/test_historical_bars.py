from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.market_data.models import (
    FetchRun,
    MarketBar,
    MarketDataProvider,
    ProviderSymbol,
)
from src.product_components.market_data.service import MarketDataService

_START = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
_END = datetime(2026, 6, 1, 9, 35, tzinfo=timezone.utc)


def _bar(minute: int) -> MarketBar:
    now = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
    return MarketBar(
        ticker="RHM",
        exchange_code="XETR",
        provider=MarketDataProvider.IBKR,
        bar_interval="1m",
        bar_start_at=now + timedelta(minutes=minute),
        currency="EUR",
        open_price=10 + minute,
        high_price=11 + minute,
        low_price=9 + minute,
        close_price=10.5 + minute,
        volume=1000,
        adjusted=False,
        fetched_at=now,
    )


class _FakeProvider:
    provider = MarketDataProvider.IBKR

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch_quote(self, symbol: ProviderSymbol):
        return None

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact"):
        return []

    def fetch_historical_bars(self, symbol, *, interval, start, end):
        self.calls.append((interval, start, end))
        return [_bar(minute) for minute in range(6)]


class _FakeStorage:
    def __init__(self, *, stored: list[MarketBar]) -> None:
        self._stored = list(stored)
        self.upserted: list[MarketBar] = []
        self.fetch_runs: list[FetchRun] = []
        self.api_usage: list[tuple[MarketDataProvider, str]] = []
        self.mapping = ProviderSymbol(
            ticker="RHM",
            exchange_code="XETR",
            provider=MarketDataProvider.IBKR,
            provider_symbol="RHM",
            currency="EUR",
        )

    def load_provider_symbols(self, *, ticker: str, exchange_code: str):
        return [self.mapping]

    def load_bars_in_range(self, *, ticker, exchange_code, bar_interval, start, end, adjusted=False):
        return sorted(
            (bar for bar in self._stored if start <= bar.bar_start_at <= end),
            key=lambda bar: bar.bar_start_at,
        )

    def upsert_bars(self, bars: list[MarketBar]) -> None:
        self.upserted.extend(bars)
        self._stored.extend(bars)

    def record_fetch_run(self, run: FetchRun) -> None:
        self.fetch_runs.append(run)

    def record_api_usage(self, *, provider, endpoint, called_at) -> None:
        self.api_usage.append((provider, endpoint))


def _service(storage: _FakeStorage, provider: _FakeProvider) -> MarketDataService:
    return MarketDataService(
        storage=storage,  # type: ignore[arg-type]
        provider_clients={MarketDataProvider.IBKR: provider},
        quote_max_age_seconds=7200,
        daily_bar_lookback_days=90,
    )


def test_get_historical_bars_returns_stored_without_provider_fetch() -> None:
    stored = [_bar(minute) for minute in range(6)]
    storage = _FakeStorage(stored=stored)
    provider = _FakeProvider()
    service = _service(storage, provider)

    bars = service.get_historical_bars(
        ticker="RHM",
        exchange_code="XETR",
        interval="1m",
        start=_START,
        end=_END,
    )

    assert provider.calls == []
    assert storage.upserted == []
    assert [bar.bar_start_at for bar in bars] == [_bar(minute).bar_start_at for minute in range(6)]


def test_get_historical_bars_backfills_gap_and_returns_sorted() -> None:
    storage = _FakeStorage(stored=[])
    provider = _FakeProvider()
    service = _service(storage, provider)

    bars = service.get_historical_bars(
        ticker="RHM",
        exchange_code="XETR",
        interval="1m",
        start=_START,
        end=_END,
    )

    assert len(provider.calls) == 1
    assert provider.calls[0] == ("1m", _START, _END)
    assert len(storage.upserted) == 6
    assert [run.operation for run in storage.fetch_runs] == ["historical_bars"]
    assert storage.api_usage == [(MarketDataProvider.IBKR, "historical_bars")]
    assert [bar.bar_start_at for bar in bars] == sorted(bar.bar_start_at for bar in bars)
    assert len(bars) == 6
