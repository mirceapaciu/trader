from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.product_components.market_data.models import (
    FetchRun,
    HistoricalBarsPrefetchOutcome,
    Instrument,
    MarketBar,
    MarketDataProvider,
    ProviderSymbol,
)
from src.product_components.market_data.historical_bars import HistoricalBarsFetchIncomplete
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
        self.coverage: dict[tuple[str, str, str, str, bool], tuple[datetime, datetime]] = {}

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

    def load_bar_coverage(
        self, *, ticker, exchange_code, provider, bar_interval, adjusted=False
    ):
        return self.coverage.get(
            (ticker.upper(), exchange_code.upper(), provider.value, bar_interval, adjusted)
        )

    def upsert_bar_coverage(
        self,
        *,
        ticker,
        exchange_code,
        provider,
        bar_interval,
        covered_start,
        covered_end,
        adjusted=False,
    ) -> None:
        key = (ticker.upper(), exchange_code.upper(), provider.value, bar_interval, adjusted)
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


def test_completed_sparse_response_reuses_authoritative_coverage() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    instruments = [Instrument(ticker="AAPL", exchange_code="XNAS")]
    service.prefetch_historical_bars(instruments, interval="1m", start=_START, end=_END)
    service.prefetch_historical_bars(instruments, interval="1m", start=_START, end=_END)

    # A successful provider return confirms the whole requested range even though
    # weekends and closed-session boundaries mean only a few bars exist.
    assert len(polygon.calls) == 1


def test_completed_coverage_is_authoritative_when_stored_bars_are_sparse() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    start = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
    storage.bars = [
        MarketBar(
            ticker="T",
            exchange_code="XNYS",
            provider=MarketDataProvider.POLYGON,
            bar_interval="1m",
            bar_start_at=start,
            currency="USD",
            open_price=10.0,
            high_price=11.0,
            low_price=9.0,
            close_price=10.5,
            volume=1000,
            adjusted=False,
            fetched_at=start,
        ),
        MarketBar(
            ticker="T",
            exchange_code="XNYS",
            provider=MarketDataProvider.POLYGON,
            bar_interval="1m",
            bar_start_at=datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc),
            currency="USD",
            open_price=10.0,
            high_price=11.0,
            low_price=9.0,
            close_price=10.5,
            volume=1000,
            adjusted=False,
            fetched_at=start,
        ),
    ]
    storage.coverage[("T", "XNYS", "polygon", "1m", False)] = (start, end)

    service.prefetch_historical_bars([("T", "XNYS")], interval="1m", start=start, end=end)

    assert polygon.calls == []


def test_sunday_to_saturday_repeat_uses_weekday_bars_and_coverage() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    start = datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)  # Sunday
    end = datetime(2026, 6, 6, 23, 59, tzinfo=timezone.utc)  # Saturday
    storage.bars = [_bar(MarketDataProvider.POLYGON, "AAPL", "XNAS", minute) for minute in range(3)]
    storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] = (start, end)
    events: list[tuple[int, int, str, str]] = []

    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")],
        interval="1m",
        start=start,
        end=end,
        progress=lambda done, total, ticker, status: events.append(
            (done, total, ticker, status)
        ),
    )

    assert outcomes[("AAPL", "XNAS")].status == "cached"
    assert events == [(1, 1, "AAPL", "cached")]
    assert polygon.calls == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        # Spring daylight-saving transition weekend and the following session.
        (
            datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 9, 23, 59, tzinfo=timezone.utc),
        ),
        # Independence Day observed: no XNYS session on Friday, July 3.
        (
            datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 3, 23, 59, tzinfo=timezone.utc),
        ),
        # Day after Thanksgiving early close, with bounds beyond regular hours.
        (
            datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 27, 23, 59, tzinfo=timezone.utc),
        ),
        # Ordinary session with pre-market and after-hours request bounds.
        (
            datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 23, 59, tzinfo=timezone.utc),
        ),
    ],
    ids=["dst", "holiday", "early-close", "outside-rth"],
)
def test_non_trading_boundaries_do_not_invalidate_completed_coverage(
    start: datetime, end: datetime
) -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] = (start, end)

    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=start, end=end
    )

    assert outcomes[("AAPL", "XNAS")].status == "cached"
    assert polygon.calls == []


def test_contained_range_reuses_larger_coverage_window() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] = (
        _START - timedelta(days=1),
        _END + timedelta(days=1),
    )

    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    assert outcomes[("AAPL", "XNAS")].status == "cached"
    assert polygon.calls == []


def test_extended_range_fetches_only_extension_then_becomes_cached() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    extended_end = _END + timedelta(days=1)
    storage.bars = [_bar(MarketDataProvider.POLYGON, "AAPL", "XNAS", 0)]
    storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] = (_START, _END)

    first = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=extended_end
    )
    second = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=extended_end
    )

    assert first[("AAPL", "XNAS")].status == "fetched"
    assert second[("AAPL", "XNAS")].status == "cached"
    assert polygon.calls == [("AAPL", _END, extended_end)]
    assert storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] == (
        _START,
        extended_end,
    )


def test_empty_completed_range_is_reused_as_cached() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    weekend_start = datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc)
    weekend_end = datetime(2026, 6, 7, 23, 59, tzinfo=timezone.utc)
    storage.coverage[("AAPL", "XNAS", "polygon", "1m", False)] = (
        weekend_start,
        weekend_end,
    )

    outcome = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=weekend_start, end=weekend_end
    )[("AAPL", "XNAS")]

    assert outcome.status == "cached"
    assert outcome.failure_category is None
    assert polygon.calls == []


def test_interrupted_response_persists_partial_bars_without_advancing_coverage() -> None:
    class _InterruptedClient(_CountingClient):
        def fetch_historical_bars(self, symbol, *, interval, start, end):
            self.calls.append((symbol.ticker, start, end))
            raise HistoricalBarsFetchIncomplete(
                "pagination stopped",
                partial_bars=[_bar(self.provider, symbol.ticker, symbol.exchange_code, 0)],
                cause=TimeoutError("page timed out"),
            )

    storage = _FakeStorage()
    polygon = _InterruptedClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    first = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )[("AAPL", "XNAS")]
    service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    assert first.status == "unavailable"
    assert first.error_code == "TimeoutError"
    assert len(storage.bars) == 2
    assert storage.coverage == {}
    assert len(polygon.calls) == 2


@pytest.mark.parametrize(
    "coverage_key",
    [
        ("AAPL", "XNAS", "ibkr", "1m", False),
        ("AAPL", "XNAS", "polygon", "5m", False),
        ("AAPL", "XNYS", "polygon", "1m", False),
        ("AAPL", "XNAS", "polygon", "1m", True),
    ],
    ids=["provider", "interval", "exchange", "adjusted"],
)
def test_incompatible_coverage_identity_is_not_reused(coverage_key) -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    storage.coverage[coverage_key] = (_START, _END)

    service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    assert polygon.calls == [("AAPL", _START, _END)]


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


def test_prefetch_uses_per_instrument_end_boundaries() -> None:
    storage = _FakeStorage()
    polygon = _CountingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    aapl_end = _END - timedelta(days=3)
    msft_end = _END - timedelta(days=1)

    service.prefetch_historical_bars(
        [("AAPL", "XNAS"), ("MSFT", "XNAS")],
        interval="1m",
        start=_START,
        end=_END,
        end_by_instrument={
            ("AAPL", "XNAS"): aapl_end,
            ("MSFT", "XNAS"): msft_end,
        },
    )

    assert {ticker: end for ticker, _start, end in polygon.calls} == {
        "AAPL": aapl_end,
        "MSFT": msft_end,
    }


def test_prefetch_reports_unavailable_when_provider_fetch_fails() -> None:
    class _FailingClient(_CountingClient):
        def fetch_historical_bars(self, symbol, *, interval, start, end):
            raise TimeoutError(
                "gateway timed out authorization=Bearer super-secret-token "
                "dsn=postgresql://alice:database-password@db/trader "
                "api_key=provider-key headers={'Cookie': 'session=cookie-value'} "
                + "x" * 400
            )

    storage = _FakeStorage()
    polygon = _FailingClient(MarketDataProvider.POLYGON)
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})

    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    outcome = outcomes[("AAPL", "XNAS")]
    assert outcome == HistoricalBarsPrefetchOutcome(
        ticker="AAPL",
        exchange_code="XNAS",
        status="unavailable",
        provider=MarketDataProvider.POLYGON,
        failure_category="provider_error",
        considered_providers=(
            MarketDataProvider.POLYGON,
            MarketDataProvider.IBKR,
            MarketDataProvider.ALPHA_VANTAGE,
        ),
        error_code="TimeoutError",
        error_message=outcome.error_message,
    )
    assert outcome.error_message is not None
    for secret in (
        "super-secret-token",
        "database-password",
        "provider-key",
        "cookie-value",
    ):
        assert secret not in outcome.error_message
    assert "<redacted>" in outcome.error_message
    assert len(outcome.error_message) <= 300
    assert storage.fetch_runs[-1].status == "failed"
    assert storage.coverage == {}


def test_prefetch_distinguishes_no_configured_provider() -> None:
    events: list[tuple[int, int, str, str]] = []
    outcomes = _service(_FakeStorage(), {}).prefetch_historical_bars(
        [("AAPL", "XNAS")],
        interval="1m",
        start=_START,
        end=_END,
        progress=lambda done, total, ticker, status: events.append(
            (done, total, ticker, status)
        ),
    )

    outcome = outcomes[("AAPL", "XNAS")]
    assert outcome.failure_category == "no_provider_configured"
    assert outcome.provider is None
    assert outcome.considered_providers == (
        MarketDataProvider.POLYGON,
        MarketDataProvider.IBKR,
        MarketDataProvider.ALPHA_VANTAGE,
    )
    assert events == [(1, 1, "AAPL", "unavailable")]


def test_prefetch_distinguishes_missing_symbol_mapping() -> None:
    ibkr = _CountingClient(MarketDataProvider.IBKR)
    outcomes = _service(
        _FakeStorage(), {MarketDataProvider.IBKR: ibkr}
    ).prefetch_historical_bars([("VOD", "XLON")], interval="1m", start=_START, end=_END)

    outcome = outcomes[("VOD", "XLON")]
    assert outcome.failure_category == "no_symbol_mapping"
    assert outcome.provider is None
    assert outcome.considered_providers == (MarketDataProvider.IBKR,)
    assert ibkr.calls == []


def test_prefetch_distinguishes_successful_empty_response() -> None:
    class _EmptyClient(_CountingClient):
        def fetch_historical_bars(self, symbol, *, interval, start, end):
            self.calls.append((symbol.ticker, start, end))
            return []

    polygon = _EmptyClient(MarketDataProvider.POLYGON)
    storage = _FakeStorage()
    service = _service(storage, {MarketDataProvider.POLYGON: polygon})
    outcomes = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )

    outcome = outcomes[("AAPL", "XNAS")]
    assert outcome.failure_category == "empty_response"
    assert outcome.provider is MarketDataProvider.POLYGON
    assert outcome.error_code is None
    assert outcome.error_message is None
    cached = service.prefetch_historical_bars(
        [("AAPL", "XNAS")], interval="1m", start=_START, end=_END
    )[("AAPL", "XNAS")]
    assert cached.status == "cached"
    assert len(polygon.calls) == 1


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
