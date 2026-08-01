from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.market_data.models import (
    FetchRun,
    Instrument,
    MarketBar,
    MarketDataProvider,
    ProviderSymbol,
)
from src.product_components.market_data.service import MarketDataService

_START = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
_END = datetime(2026, 6, 30, 20, 0, tzinfo=timezone.utc)


def _bar(provider: MarketDataProvider, ticker: str, exchange: str, minute: int) -> MarketBar:
    return MarketBar(
        ticker=ticker,
        exchange_code=exchange,
        provider=provider,
        bar_interval="1m",
        bar_start_at=_START + timedelta(minutes=minute),
        currency="USD",
        open_price=10.0,
        high_price=11.0,
        low_price=9.0,
        close_price=10.5,
        volume=1000,
        adjusted=False,
        fetched_at=_START,
    )


class _CountingClient:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider
        self.calls: list[tuple[str, datetime, datetime]] = []

    def fetch_quote(self, symbol):
        return None

    def fetch_daily_bars(self, symbol, *, outputsize: str = "compact"):
        return []

    def fetch_historical_bars(self, symbol, *, interval, start, end):
        self.calls.append((symbol.ticker, start, end))
        return [_bar(self.provider, symbol.ticker, symbol.exchange_code, m) for m in range(3)]


class _FakeStorage:
    def __init__(self) -> None:
        self.bars: list[MarketBar] = []
        self.fetch_runs: list[FetchRun] = []
        self.api_usage: list[tuple[MarketDataProvider, str]] = []
        self.provider_symbols: list[ProviderSymbol] = []
        self.coverage: dict[tuple[str, str, str, str], tuple[datetime, datetime]] = {}

    def load_provider_symbols(self, *, ticker, exchange_code, provider=None):
        return [
            symbol
            for symbol in self.provider_symbols
            if symbol.ticker == ticker.upper() and symbol.exchange_code == exchange_code.upper()
        ]

    def upsert_provider_symbol(self, symbol: ProviderSymbol) -> None:
        self.provider_symbols = [
            existing
            for existing in self.provider_symbols
            if (existing.ticker, existing.exchange_code, existing.provider) != (
                symbol.ticker,
                symbol.exchange_code,
                symbol.provider,
            )
        ]
        self.provider_symbols.append(symbol)

    def load_bars_in_range(self, *, ticker, exchange_code, bar_interval, start, end, adjusted=False):
        return sorted(
            (
                bar
                for bar in self.bars
                if bar.ticker == ticker.upper()
                and bar.exchange_code == exchange_code.upper()
                and start <= bar.bar_start_at <= end
            ),
            key=lambda bar: bar.bar_start_at,
        )

    def upsert_bars(self, bars: list[MarketBar]) -> None:
        self.bars.extend(bars)

    def record_fetch_run(self, run: FetchRun) -> None:
        self.fetch_runs.append(run)

    def record_api_usage(self, *, provider, endpoint, called_at) -> None:
        self.api_usage.append((provider, endpoint))

    def load_bar_coverage(self, *, ticker, exchange_code, provider, bar_interval):
        return self.coverage.get((ticker.upper(), exchange_code.upper(), provider.value, bar_interval))

    def upsert_bar_coverage(
        self, *, ticker, exchange_code, provider, bar_interval, covered_start, covered_end
    ) -> None:
        key = (ticker.upper(), exchange_code.upper(), provider.value, bar_interval)
        existing = self.coverage.get(key)
        if existing is None:
            self.coverage[key] = (covered_start, covered_end)
        else:
            self.coverage[key] = (min(existing[0], covered_start), max(existing[1], covered_end))


def _service(storage, clients, **kwargs) -> MarketDataService:
    kwargs.setdefault("sleep", lambda _seconds: None)
    return MarketDataService(
        storage=storage,  # type: ignore[arg-type]
        provider_clients=clients,
        quote_max_age_seconds=7200,
        daily_bar_lookback_days=90,
        **kwargs,
    )


class _AvailableIbkrClient(_CountingClient):
    def __init__(self, available: bool) -> None:
        super().__init__(MarketDataProvider.IBKR)
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _preference(clients, *, prefer_ibkr: bool = True) -> "MarketDataService":
    return MarketDataService(
        storage=None,  # type: ignore[arg-type]  # _provider_preference does not touch storage
        provider_clients=clients,
        quote_max_age_seconds=1,
        daily_bar_lookback_days=1,
        prefer_ibkr_historical=prefer_ibkr,
    )


def test_provider_preference_puts_ibkr_first_when_available() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=True),
        }
    )
    assert service._provider_preference("XNAS") == [
        MarketDataProvider.IBKR,
        MarketDataProvider.POLYGON,
        MarketDataProvider.ALPHA_VANTAGE,
    ]


def test_provider_preference_keeps_polygon_first_when_ibkr_unavailable() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=False),
        }
    )
    assert service._provider_preference("XNAS")[0] is MarketDataProvider.POLYGON


def test_provider_preference_respects_disabled_toggle_even_when_available() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=True),
        },
        prefer_ibkr=False,
    )
    assert service._provider_preference("XNAS")[0] is MarketDataProvider.POLYGON


def test_quote_preference_us_with_live_ibkr_tries_ibkr_then_polygon() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=True),
        }
    )
    assert service._quote_provider_preference("XNAS") == [
        MarketDataProvider.IBKR,
        MarketDataProvider.POLYGON,
    ]


def test_quote_preference_us_with_dead_ibkr_skips_to_polygon() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=False),
        }
    )
    assert service._quote_provider_preference("XNAS") == [MarketDataProvider.POLYGON]


def test_quote_preference_non_us_is_ibkr_only() -> None:
    service = _preference(
        {
            MarketDataProvider.POLYGON: _CountingClient(MarketDataProvider.POLYGON),
            MarketDataProvider.IBKR: _AvailableIbkrClient(available=True),
        }
    )
    assert service._quote_provider_preference("XETR") == [MarketDataProvider.IBKR]


def test_us_instrument_prefers_ibkr_when_available() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    ibkr = _AvailableIbkrClient(available=True)
    service = _service(
        storage,
        {MarketDataProvider.POLYGON: polygon, MarketDataProvider.IBKR: ibkr},
    )

    service.get_historical_bars(
        ticker="AAPL", exchange_code="XNAS", interval="1m", start=_START, end=_END
    )

    assert len(ibkr.calls) == 1
    assert polygon.calls == []
    assert any(s.provider is MarketDataProvider.IBKR for s in storage.provider_symbols)


def test_us_instrument_falls_back_to_polygon_when_ibkr_unavailable() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    ibkr = _AvailableIbkrClient(available=False)
    service = _service(
        storage,
        {MarketDataProvider.POLYGON: polygon, MarketDataProvider.IBKR: ibkr},
    )

    service.get_historical_bars(
        ticker="AAPL", exchange_code="XNAS", interval="1m", start=_START, end=_END
    )

    # Guards the coverage-ledger trap: a disconnected IBKR must not win and mark the
    # window covered-but-empty; Polygon serves the bars instead.
    assert len(polygon.calls) == 1
    assert ibkr.calls == []


def test_us_instrument_routes_to_polygon() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    ibkr = _CountingClient(MarketDataProvider.IBKR)
    service = _service(
        storage,
        {MarketDataProvider.POLYGON: polygon, MarketDataProvider.IBKR: ibkr},
    )

    service.get_historical_bars(
        ticker="AAPL", exchange_code="XNAS", interval="1m", start=_START, end=_END
    )

    assert len(polygon.calls) == 1
    assert ibkr.calls == []
    # The discovered Polygon mapping is persisted for future lookups.
    assert any(s.provider is MarketDataProvider.POLYGON for s in storage.provider_symbols)


def test_non_us_instrument_routes_to_ibkr() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    ibkr = _CountingClient(MarketDataProvider.IBKR)
    service = _service(
        storage,
        {MarketDataProvider.POLYGON: polygon, MarketDataProvider.IBKR: ibkr},
    )

    service.get_historical_bars(
        ticker="RHM", exchange_code="XETR", interval="1m", start=_START, end=_END
    )

    assert len(ibkr.calls) == 1
    assert polygon.calls == []


def test_coverage_ledger_prevents_refetch() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    instruments = [Instrument(ticker="AAPL", exchange_code="XNAS")]
    service.prefetch_historical_bars(instruments, interval="1m", start=_START, end=_END)
    service.prefetch_historical_bars(instruments, interval="1m", start=_START, end=_END)

    # Second pass is fully served by the coverage ledger: no extra provider request.
    assert len(polygon.calls) == 1


def test_prefetch_reports_progress_and_dedupes() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    events: list[tuple[int, int, str, str]] = []
    service.prefetch_historical_bars(
        [
            ("AAPL", "XNAS"),
            ("MSFT", "XNAS"),
            ("aapl", "xnas"),  # duplicate of AAPL after normalization
        ],
        interval="1m",
        start=_START,
        end=_END,
        progress=lambda done, total, ticker, status: events.append((done, total, ticker, status)),
    )

    assert [e[1] for e in events] == [2, 2]  # deduped to two instruments
    assert {e[3] for e in events} == {"fetched"}
    assert len(polygon.calls) == 2


def test_prefetch_reports_unavailable_when_provider_fetch_fails() -> None:
    class _FailingClient(_CountingClient):
        def fetch_historical_bars(self, symbol, *, interval, start, end):
            raise TimeoutError("gateway timed out")

    storage = _FakeStorage()
    polygon = _FailingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    assert outcomes == {("AAPL", "XNAS"): "unavailable"}
    assert storage.fetch_runs[-1].status == "failed"


def test_rate_limiter_spaces_provider_calls() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)

    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    sleeps: list[float] = []
    service = _service(
        storage,
        {MarketDataProvider.POLYGON: polygon},
        max_requests_per_minute=5,
        clock=lambda: next(ticks),
        sleep=sleeps.append,
    )

    service.prefetch_historical_bars(
        [("AAPL", "XNAS"), ("MSFT", "XNAS"), ("NVDA", "XNAS")],
        interval="1m",
        start=_START,
        end=_END,
    )

    # 5 req/min => 12s minimum spacing; first call is free, the next two each wait.
    assert sleeps == [12.0, 12.0]
