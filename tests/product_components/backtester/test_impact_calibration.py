from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.product_components.backtester.impact_calibration import (
    DailyBar,
    ImpactObservation,
    atr_20d_from_bars,
    build_impact_calibration_report,
    compute_realized_moves,
    format_impact_calibration_markdown,
)

_UTC = timezone.utc


def _bar(day: datetime, close: float, *, spread: float = 1.0) -> DailyBar:
    return DailyBar(start_at=day, high=close + spread, low=close - spread, close=close)


def _weekday_bars(start: datetime, closes: list[float]) -> list[DailyBar]:
    """Consecutive weekday sessions starting at ``start`` (skips Sat/Sun)."""
    bars = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(_bar(day, close))
        day += timedelta(days=1)
    return bars


def test_compute_realized_moves_uses_prior_session_baseline() -> None:
    # Mon 100, Tue 102, Wed 104, Thu 106, Fri 108, Mon 110, Tue 112
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104, 106, 108, 110, 112])
    published = datetime(2026, 7, 8, 14, 30, tzinfo=_UTC)  # Wednesday intraday

    moves = compute_realized_moves(
        bars, published_at=published, atr_20d=2.0, direction="buy", horizons_sessions=(1, 5)
    )

    # Baseline is Tuesday's close (102); 1 session later = Wednesday close (104).
    assert moves[1] == pytest.approx((104 - 102) / 2.0)
    # 5 sessions later = the second Tuesday close (112).
    assert moves[5] == pytest.approx((112 - 102) / 2.0)


def test_compute_realized_moves_weekend_publication_uses_friday_baseline() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104, 106, 108, 110])
    published = datetime(2026, 7, 11, 9, 0, tzinfo=_UTC)  # Saturday

    moves = compute_realized_moves(
        bars, published_at=published, atr_20d=2.0, direction="buy", horizons_sessions=(1,)
    )

    # Baseline Friday (108); next session is Monday (110).
    assert moves[1] == pytest.approx((110 - 108) / 2.0)


def test_compute_realized_moves_sell_direction_flips_sign() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104])
    published = datetime(2026, 7, 7, 12, 0, tzinfo=_UTC)  # Tuesday

    buy = compute_realized_moves(
        bars, published_at=published, atr_20d=1.0, direction="buy", horizons_sessions=(1,)
    )
    sell = compute_realized_moves(
        bars, published_at=published, atr_20d=1.0, direction="sell", horizons_sessions=(1,)
    )

    # Price rose after publication: aligned for buy, anti-aligned for sell.
    assert buy[1] == pytest.approx(2.0)
    assert sell[1] == pytest.approx(-2.0)


def test_compute_realized_moves_horizon_beyond_data_is_none() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104])
    published = datetime(2026, 7, 7, 12, 0, tzinfo=_UTC)  # Tuesday

    moves = compute_realized_moves(
        bars, published_at=published, atr_20d=1.0, direction="buy", horizons_sessions=(1, 5)
    )

    assert moves[1] == pytest.approx(2.0)
    assert moves[5] is None


def test_compute_realized_moves_no_baseline_returns_none() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104])
    published = datetime(2026, 7, 5, 12, 0, tzinfo=_UTC)  # before any bar

    moves = compute_realized_moves(
        bars, published_at=published, atr_20d=1.0, direction="buy", horizons_sessions=(1,)
    )

    assert moves[1] is None


def test_compute_realized_moves_benchmark_subtracts_market_move() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104])
    # Benchmark rises 1% over the same window.
    benchmark = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [500, 505, 510])
    published = datetime(2026, 7, 7, 12, 0, tzinfo=_UTC)

    raw = compute_realized_moves(
        bars, published_at=published, atr_20d=2.0, direction="buy", horizons_sessions=(1,)
    )
    excess = compute_realized_moves(
        bars,
        published_at=published,
        atr_20d=2.0,
        direction="buy",
        horizons_sessions=(1,),
        benchmark_bars=benchmark,
    )

    # Tuesday publication -> Monday baselines: instrument moved (102-100)/2 = 1.0
    # ATR; benchmark moved (505-500)/500 = 1%, which in instrument ATR units is
    # 0.01 * 100 / 2.0 = 0.5.
    expected_benchmark_atr = (505 - 500) / 500 * 100 / 2.0
    assert raw[1] == pytest.approx(1.0)
    assert excess[1] == pytest.approx(1.0 - expected_benchmark_atr)


def test_compute_realized_moves_benchmark_gap_skips_observation() -> None:
    bars = _weekday_bars(datetime(2026, 7, 6, tzinfo=_UTC), [100, 102, 104])
    published = datetime(2026, 7, 7, 12, 0, tzinfo=_UTC)

    moves = compute_realized_moves(
        bars,
        published_at=published,
        atr_20d=2.0,
        direction="buy",
        horizons_sessions=(1,),
        benchmark_bars=[],  # requested but unavailable
    )

    assert moves[1] is None


def test_atr_20d_from_bars_matches_engine_formula() -> None:
    # 25 sessions of constant close and constant 2.0 high-low spread: every true
    # range is exactly 2.0, so ATR must be 2.0.
    bars = _weekday_bars(datetime(2026, 6, 1, tzinfo=_UTC), [100.0] * 25)
    atr = atr_20d_from_bars(bars, before=datetime(2026, 7, 6, tzinfo=_UTC))

    assert atr == pytest.approx(2.0)


def test_atr_20d_from_bars_requires_21_sessions() -> None:
    bars = _weekday_bars(datetime(2026, 6, 1, tzinfo=_UTC), [100.0] * 20)
    assert atr_20d_from_bars(bars, before=datetime(2026, 7, 6, tzinfo=_UTC)) is None


def _observation(
    idx: int, magnitude: str, move_1: float | None, move_5: float | None = None, **overrides
) -> ImpactObservation:
    values = dict(
        analysis_id=idx,
        ticker="AAPL",
        exchange_code="XNAS",
        published_at=datetime(2026, 7, 7, tzinfo=_UTC),
        direction="buy",
        event_type="earnings",
        magnitude=magnitude,
        impact_horizon="1d",
        atr_20d=2.0,
        moves_atr={1: move_1, 5: move_5},
    )
    values.update(overrides)
    return ImpactObservation(**values)


def test_report_buckets_and_monotonicity() -> None:
    observations = [
        _observation(1, "low", 0.1, 0.2),
        _observation(2, "low", 0.3, 0.1),
        _observation(3, "medium", 0.8, 1.0),
        _observation(4, "medium", 1.0, 0.8),
        _observation(5, "high", 1.8, 2.4),
        _observation(6, "high", -0.5, 2.0),
    ]

    report = build_impact_calibration_report(observations, min_sample_size=2)

    assert report.observation_count == 6
    by_magnitude = {bucket.label: bucket for bucket in report.by_magnitude}
    assert set(by_magnitude) == {"low", "medium", "high"}
    low = by_magnitude["low"]
    assert low.observation_count == 2
    assert not low.sample_too_small
    assert low.horizons[1].median_move_atr == pytest.approx(0.2)
    assert low.horizons[1].hit_rate == pytest.approx(1.0)
    high = by_magnitude["high"]
    assert high.horizons[1].hit_rate == pytest.approx(0.5)
    # Medians: low 0.2 < medium 0.9 < high 0.65 -> NOT monotonic at 1 session.
    assert report.monotonic_by_horizon[1] is False
    # At 5 sessions: 0.15 < 0.9 < 2.2 -> monotonic.
    assert report.monotonic_by_horizon[5] is True


def test_report_flags_small_samples_and_handles_missing_moves() -> None:
    observations = [
        _observation(1, "high", None, None),  # no bars/ATR -> excluded from stats
        _observation(2, "high", 1.2, None),
    ]

    report = build_impact_calibration_report(observations, min_sample_size=30)

    bucket = report.by_magnitude[0]
    assert bucket.observation_count == 2
    assert bucket.sample_too_small
    assert bucket.horizons[1].observation_count == 1
    assert bucket.horizons[5].observation_count == 0
    assert bucket.horizons[5].median_move_atr is None
    # Missing magnitudes leave monotonicity undecided.
    assert report.monotonic_by_horizon[1] is None


def test_report_groups_by_event_type_and_horizon() -> None:
    observations = [
        _observation(1, "high", 1.0, event_type="earnings", impact_horizon="1d"),
        _observation(2, "high", 2.0, event_type="m_and_a", impact_horizon="5d"),
        _observation(3, "low", 0.1, event_type=None, impact_horizon=None),
    ]

    report = build_impact_calibration_report(observations, min_sample_size=1)

    event_labels = [bucket.label for bucket in report.by_event_type_magnitude]
    assert event_labels == ["earnings / high", "m_and_a / high", "unknown / low"]
    horizon_labels = [bucket.label for bucket in report.by_magnitude_horizon]
    assert horizon_labels == ["high / 1d", "high / 5d", "low / unknown"]


def test_markdown_report_renders() -> None:
    observations = [
        _observation(1, "low", 0.1, 0.2),
        _observation(2, "high", 1.8, 2.4),
    ]

    report = build_impact_calibration_report(observations, min_sample_size=5)
    markdown = format_impact_calibration_markdown(report)

    assert "# Impact Calibration Report" in markdown
    assert "## By magnitude" in markdown
    assert "insufficient sample" in markdown
    assert "+1.80" in markdown

    payload = report.to_dict()
    assert payload["observation_count"] == 2
    assert payload["by_magnitude"][0]["label"] == "low"
