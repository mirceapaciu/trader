"""Unit tests for backtest performance metric helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core_components.backtest_engine.metrics import (
    expectancy,
    max_drawdown,
    max_drawdown_with_duration,
    percentile,
    profit_factor,
    round_half_up,
    sharpe_ratio,
    win_rate,
)

UTC = timezone.utc


def test_round_half_up():
    assert round_half_up(2.5, 0) == 3.0
    assert round_half_up(0.125, 2) == 0.13  # banker's would give 0.12
    assert round_half_up(1.005, 2) == 1.01
    assert round_half_up(-2.5, 0) == -3.0  # ROUND_HALF_UP rounds away from zero


def test_percentile_empty():
    assert percentile([], 50) is None


def test_percentile_single():
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 0) == 7.0
    assert percentile([7.0], 100) == 7.0


def test_percentile_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0) == pytest.approx(1.0)
    assert percentile(values, 100) == pytest.approx(4.0)
    assert percentile(values, 50) == pytest.approx(2.5)
    # rank = 0.25 * 3 = 0.75 -> 1 + 0.75*(2-1) = 1.75
    assert percentile(values, 25) == pytest.approx(1.75)


def test_percentile_unsorted_input():
    assert percentile([4.0, 1.0, 3.0, 2.0], 50) == pytest.approx(2.5)


def test_win_rate():
    assert win_rate(3, 4) == pytest.approx(0.75)
    assert win_rate(0, 0) is None
    assert win_rate(0, 5) == pytest.approx(0.0)


def test_profit_factor():
    assert profit_factor(200.0, 100.0) == pytest.approx(2.0)
    assert profit_factor(0.0, 0.0) is None
    assert profit_factor(200.0, 0.0) is None  # zero loss -> None even with profit
    assert profit_factor(0.0, 50.0) == pytest.approx(0.0)


def test_expectancy():
    assert expectancy(500.0, 10) == pytest.approx(50.0)
    assert expectancy(0.0, 0) is None
    assert expectancy(-100.0, 4) == pytest.approx(-25.0)


def test_sharpe_fewer_than_two():
    assert sharpe_ratio([]) is None
    assert sharpe_ratio([0.01]) is None


def test_sharpe_zero_stdev():
    assert sharpe_ratio([0.01, 0.01, 0.01]) is None


def test_sharpe_basic():
    # returns 0.0 and 0.02 -> excess mean 0.01, population stdev 0.01 -> 1.0
    assert sharpe_ratio([0.0, 0.02]) == pytest.approx(1.0)


def test_sharpe_with_risk_free():
    # subtract rf 0.01 from each -> [-0.01, 0.01], mean 0, stdev 0.01 -> 0.0
    assert sharpe_ratio([0.0, 0.02], risk_free_per_period=0.01) == pytest.approx(0.0)


def test_max_drawdown_too_few_points():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100.0]) == 0.0


def test_max_drawdown_never_declines():
    assert max_drawdown([100.0, 110.0, 120.0]) == 0.0


def test_max_drawdown_known_curve():
    # peak 100 -> trough 50 = 0.5 drawdown
    curve = [100.0, 80.0, 50.0, 70.0, 60.0]
    assert max_drawdown(curve) == pytest.approx(0.5)


def test_max_drawdown_recovers_then_new_drop():
    # 100 -> 90 (0.10), recover to 200, drop to 150 (0.25) -> max 0.25
    curve = [100.0, 90.0, 200.0, 150.0]
    assert max_drawdown(curve) == pytest.approx(0.25)


def test_max_drawdown_with_duration_no_drawdown():
    points = [
        (datetime(2024, 1, 1, tzinfo=UTC), 100.0),
        (datetime(2024, 1, 2, tzinfo=UTC), 110.0),
    ]
    assert max_drawdown_with_duration(points) == (0.0, 0.0)


def test_max_drawdown_with_duration_too_few():
    assert max_drawdown_with_duration([]) == (0.0, 0.0)
    assert max_drawdown_with_duration(
        [(datetime(2024, 1, 1, tzinfo=UTC), 100.0)]
    ) == (0.0, 0.0)


def test_max_drawdown_with_duration_known_curve():
    base = datetime(2024, 1, 1, tzinfo=UTC)
    points = [
        (base, 100.0),  # peak
        (base + timedelta(hours=1), 80.0),
        (base + timedelta(hours=2), 50.0),  # trough of max dd
        (base + timedelta(hours=3), 70.0),
    ]
    fraction, duration = max_drawdown_with_duration(points)
    assert fraction == pytest.approx(0.5)
    # peak at hour 0, trough at hour 2 -> 7200 seconds
    assert duration == pytest.approx(7200.0)
