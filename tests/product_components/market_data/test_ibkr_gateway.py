from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.product_components.market_data import ibkr_gateway
from src.product_components.market_data.ibkr_gateway import (
    _BAR_SIZE_SETTING,
    IbAsyncMarketDataGateway,
    _bar_datetime,
    _duration_str,
    build_market_data_ibkr_gateway,
)


# --- pure helpers ------------------------------------------------------------


def test_bar_size_setting_covers_all_supported_intervals() -> None:
    assert set(_BAR_SIZE_SETTING) == {"1m", "5m", "15m", "30m", "1h", "1d"}


def test_duration_str_uses_seconds_under_a_day_and_days_above() -> None:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _duration_str(start, start + timedelta(hours=2)) == "7200 S"
    assert _duration_str(start, start + timedelta(days=1)) == "1 D"
    # Rounds partial days up so the window is fully covered.
    assert _duration_str(start, start + timedelta(days=2, hours=3)) == "3 D"


def test_bar_datetime_normalizes_datetime_date_and_string_to_utc() -> None:
    aware = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
    assert _bar_datetime(aware) == aware
    # Naive datetimes are assumed UTC.
    assert _bar_datetime(datetime(2026, 6, 1, 13, 30)) == aware
    # Daily bars arrive as a date -> midnight UTC.
    assert _bar_datetime(date(2026, 6, 1)) == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _bar_datetime("2026-06-01T13:30:00Z") == aware
    assert _bar_datetime(None) is None
    assert _bar_datetime("not-a-date") is None


# --- historical_bars chunk stitching -----------------------------------------


class _FakeBar:
    def __init__(self, dt: datetime, price: float) -> None:
        self.date = dt
        self.open = price
        self.high = price + 1
        self.low = price - 1
        self.close = price + 0.5
        self.volume = 100.0


def _parse_duration(duration_str: str) -> timedelta:
    amount, unit = duration_str.split()
    return timedelta(seconds=int(amount)) if unit == "S" else timedelta(days=int(amount))


class _FakeIB:
    """Returns the fake series bars falling within each requested backward window."""

    def __init__(self, series: list[datetime]) -> None:
        self._series = sorted(series)
        self.request_windows: list[tuple[datetime, str]] = []

    def reqHistoricalDataAsync(
        self, contract, *, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH, formatDate
    ):
        self.request_windows.append((endDateTime, durationStr))
        window_start = endDateTime - _parse_duration(durationStr)
        return [_FakeBar(dt, 10.0) for dt in self._series if window_start <= dt <= endDateTime]


def _gateway_with_fake_ib(fake: _FakeIB) -> IbAsyncMarketDataGateway:
    gateway = IbAsyncMarketDataGateway(host="h", port=1, client_id=9)
    gateway._ib = fake  # type: ignore[assignment]
    gateway._call = lambda factory, timeout=None: factory()  # type: ignore[assignment]
    gateway._qualified_contract = lambda provider_symbol, contract_metadata: object()  # type: ignore[assignment]
    return gateway


def test_historical_bars_pages_backward_and_dedupes(monkeypatch) -> None:
    monkeypatch.setattr(ibkr_gateway.time, "sleep", lambda _s: None)
    base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    series = [base + timedelta(days=d) for d in range(5)]  # one bar/day, 1-day chunks force paging
    fake = _FakeIB(series)
    gateway = _gateway_with_fake_ib(fake)

    bars = gateway.historical_bars(
        provider_symbol="AAPL",
        interval="1m",
        start=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 5, 11, 0, tzinfo=timezone.utc),
        contract_metadata={"exchange": "SMART", "currency": "USD"},
    )

    assert [b["bar_start_at"] for b in bars] == series  # all five, sorted, deduped
    assert len(fake.request_windows) >= 5  # walked backward in multiple chunks
    assert bars[0]["open"] == 10.0 and bars[0]["close"] == 10.5 and bars[0]["volume"] == 100.0


def test_historical_bars_filters_out_of_range_bars(monkeypatch) -> None:
    monkeypatch.setattr(ibkr_gateway.time, "sleep", lambda _s: None)
    start = datetime(2026, 6, 2, tzinfo=timezone.utc)
    end = datetime(2026, 6, 3, tzinfo=timezone.utc)
    # A bar before the requested start must be dropped even if IBKR returns it.
    fake = _FakeIB([start - timedelta(days=1), end])
    gateway = _gateway_with_fake_ib(fake)

    bars = gateway.historical_bars(
        provider_symbol="AAPL", interval="1d", start=start, end=end, contract_metadata={}
    )

    assert [b["bar_start_at"] for b in bars] == [end]


def test_historical_bars_returns_empty_when_contract_unresolved(monkeypatch) -> None:
    gateway = IbAsyncMarketDataGateway(host="h", port=1, client_id=9)
    gateway._qualified_contract = lambda provider_symbol, contract_metadata: None  # type: ignore[assignment]

    bars = gateway.historical_bars(
        provider_symbol="AAPL",
        interval="1m",
        start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 2, tzinfo=timezone.utc),
        contract_metadata={},
    )
    assert bars == []


# --- build helper ------------------------------------------------------------


class _Settings:
    ibkr_host = "127.0.0.1"
    ibkr_port = 7497
    ibkr_market_data_client_id = 2
    prefer_ibkr_historical = True


def test_build_gateway_returns_none_when_ibkr_disabled() -> None:
    settings = _Settings()
    settings.prefer_ibkr_historical = False
    assert build_market_data_ibkr_gateway(settings) is None
