"""Unit tests for the BarSeries time-ordered view."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core_components.backtest_engine.bars import Bar, BarSeries

UTC = timezone.utc


def _bar(minute: int, close: float) -> Bar:
    return Bar(
        start_at=datetime(2024, 1, 1, 0, minute, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
    )


def test_empty_series():
    series = BarSeries([])
    assert series.is_empty
    assert len(series) == 0
    assert series.last() is None
    assert series.first_at_or_after(datetime(2024, 1, 1, tzinfo=UTC)) is None
    assert list(series.iter_from(datetime(2024, 1, 1, tzinfo=UTC))) == []


def test_sorted_ascending():
    series = BarSeries([_bar(3, 3), _bar(1, 1), _bar(2, 2)])
    closes = [b.close for b in series.iter_from(datetime(2024, 1, 1, tzinfo=UTC))]
    assert closes == [1, 2, 3]
    assert len(series) == 3


def test_duplicate_start_at_keeps_last():
    series = BarSeries([_bar(1, 10), _bar(1, 99), _bar(2, 20)])
    assert len(series) == 2
    first = series.first_at_or_after(datetime(2024, 1, 1, 0, 1, tzinfo=UTC))
    assert first is not None
    assert first.close == 99


def test_first_at_or_after_boundary():
    series = BarSeries([_bar(1, 1), _bar(2, 2), _bar(3, 3)])
    ts2 = datetime(2024, 1, 1, 0, 2, tzinfo=UTC)
    # at_or_after includes an exact match
    bar = series.first_at_or_after(ts2)
    assert bar is not None and bar.close == 2
    # before first bar -> earliest
    before = series.first_at_or_after(datetime(2023, 1, 1, tzinfo=UTC))
    assert before is not None and before.close == 1
    # after last bar -> None
    assert series.first_at_or_after(datetime(2025, 1, 1, tzinfo=UTC)) is None


def test_iter_from_vs_iter_after_at_exact_timestamp():
    series = BarSeries([_bar(1, 1), _bar(2, 2), _bar(3, 3)])
    ts2 = datetime(2024, 1, 1, 0, 2, tzinfo=UTC)
    from_closes = [b.close for b in series.iter_from(ts2)]
    after_closes = [b.close for b in series.iter_after(ts2)]
    assert from_closes == [2, 3]  # inclusive
    assert after_closes == [3]  # exclusive


def test_last():
    series = BarSeries([_bar(1, 1), _bar(3, 3), _bar(2, 2)])
    last = series.last()
    assert last is not None and last.close == 3


def test_naive_datetime_treated_as_utc():
    series = BarSeries([_bar(1, 1), _bar(2, 2)])
    naive = datetime(2024, 1, 1, 0, 2)  # no tzinfo
    bar = series.first_at_or_after(naive)
    assert bar is not None and bar.close == 2


def test_naive_bar_start_at_treated_as_utc():
    naive_bar = Bar(
        start_at=datetime(2024, 1, 1, 0, 5),
        open=5,
        high=5,
        low=5,
        close=5,
    )
    series = BarSeries([naive_bar])
    aware = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    bar = series.first_at_or_after(aware)
    assert bar is not None and bar.close == 5


def test_non_utc_timezone_converted():
    # 00:05 UTC bar; query at 01:05 in UTC+1 == same instant
    series = BarSeries([_bar(5, 5)])
    plus_one = timezone(timedelta(hours=1))
    ts = datetime(2024, 1, 1, 1, 5, tzinfo=plus_one)
    bar = series.first_at_or_after(ts)
    assert bar is not None and bar.close == 5
