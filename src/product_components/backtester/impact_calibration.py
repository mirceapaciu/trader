"""Predicted-impact calibration: does the LLM's price_impact_magnitude separate
realized direction-aligned moves (in ATR_20d units)?

Pure computation only (mirrors confidence_calibration.py); loading analyses and
bars is the report CLI's job. Works on article-level analysis rows rather than
backtest trades, so rejected analyses participate too — restricting to trades
would only measure the survivors of the card/admission funnel.

V1 uses daily bars only: the "intraday" horizon is approximated by the first
session close after publication, and the baseline session is chosen by calendar
date (an article published after the close on day D uses D-1 as baseline).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, median, quantiles
from typing import Any, Sequence

MAGNITUDES = ("low", "medium", "high")
IMPACT_HORIZONS = ("intraday", "1d", "5d")
HORIZON_SESSIONS = (1, 5)
# impact_horizon label -> sessions after the baseline bar at which it is evaluated.
HORIZON_LABEL_SESSIONS = {"intraday": 1, "1d": 1, "5d": 5}
DEFAULT_MIN_SAMPLE_SIZE = 30
# Bars needed for the ATR fallback: 21 dailies yield 20 true ranges.
_ATR_BARS = 21


@dataclass(frozen=True)
class DailyBar:
    """Minimal daily bar; the CLI maps provider bar models onto this."""

    start_at: datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ImpactObservation:
    analysis_id: int
    ticker: str
    exchange_code: str
    published_at: datetime
    direction: str
    event_type: str | None
    magnitude: str
    impact_horizon: str | None
    atr_20d: float | None
    # sessions after baseline -> direction-aligned move in ATR_20d units
    # (None when bars/ATR were insufficient).
    moves_atr: dict[int, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class HorizonStats:
    observation_count: int
    hit_rate: float | None
    mean_move_atr: float | None
    median_move_atr: float | None
    p25_move_atr: float | None
    p75_move_atr: float | None


@dataclass(frozen=True)
class ImpactBucket:
    label: str
    observation_count: int
    sample_too_small: bool
    horizons: dict[int, HorizonStats]


@dataclass(frozen=True)
class ImpactCalibrationReport:
    observation_count: int
    min_sample_size: int
    by_magnitude: tuple[ImpactBucket, ...]
    by_event_type_magnitude: tuple[ImpactBucket, ...]
    by_magnitude_horizon: tuple[ImpactBucket, ...]
    # sessions -> whether median moves are strictly increasing low < medium < high
    # (None when a magnitude bucket has no data at that horizon).
    monotonic_by_horizon: dict[int, bool | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_count": self.observation_count,
            "min_sample_size": self.min_sample_size,
            "monotonic_by_horizon": {
                str(sessions): value for sessions, value in self.monotonic_by_horizon.items()
            },
            "by_magnitude": [_bucket_dict(bucket) for bucket in self.by_magnitude],
            "by_event_type_magnitude": [
                _bucket_dict(bucket) for bucket in self.by_event_type_magnitude
            ],
            "by_magnitude_horizon": [
                _bucket_dict(bucket) for bucket in self.by_magnitude_horizon
            ],
        }


def atr_20d_from_bars(bars: Sequence[DailyBar], *, before: datetime) -> float | None:
    """Point-in-time 20d ATR from daily bars (same formula as the engine's
    _atr_20d_before_entry): mean true range of the last 20 sessions before ``before``."""
    ordered = sorted(
        (bar for bar in bars if bar.start_at.date() < before.date()),
        key=lambda bar: bar.start_at,
    )
    if len(ordered) < _ATR_BARS:
        return None
    true_ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(ordered[-_ATR_BARS:-1], ordered[-(_ATR_BARS - 1):])
    ]
    return sum(true_ranges) / (_ATR_BARS - 1)


def compute_realized_moves(
    bars: Sequence[DailyBar],
    *,
    published_at: datetime,
    atr_20d: float,
    direction: str,
    horizons_sessions: tuple[int, ...] = HORIZON_SESSIONS,
    benchmark_bars: Sequence[DailyBar] | None = None,
) -> dict[int, float | None]:
    """Direction-aligned realized move in ATR units at each horizon.

    Baseline = close of the last session dated strictly before ``published_at``;
    horizon k = the k-th bar after the baseline bar in the daily sequence (so
    weekends/holidays are skipped by construction). With ``benchmark_bars`` the
    benchmark's same-window percent move (converted to instrument ATR units via
    the baseline price) is subtracted before direction alignment.
    """
    if atr_20d <= 0:
        return {sessions: None for sessions in horizons_sessions}
    sign = -1.0 if direction == "sell" else 1.0
    ordered = sorted(bars, key=lambda bar: bar.start_at)
    baseline_index = _baseline_index(ordered, published_at)
    if baseline_index is None:
        return {sessions: None for sessions in horizons_sessions}
    baseline_close = ordered[baseline_index].close

    moves: dict[int, float | None] = {}
    for sessions in horizons_sessions:
        target_index = baseline_index + sessions
        if target_index >= len(ordered):
            moves[sessions] = None
            continue
        move_atr = (ordered[target_index].close - baseline_close) / atr_20d
        benchmark_move_atr = _benchmark_move_atr(
            benchmark_bars,
            published_at=published_at,
            sessions=sessions,
            instrument_baseline_close=baseline_close,
            atr_20d=atr_20d,
        )
        if benchmark_bars is not None and benchmark_move_atr is None:
            # Benchmark requested but not computable for this window: skip the
            # observation rather than silently mixing excess and raw moves.
            moves[sessions] = None
            continue
        moves[sessions] = sign * (move_atr - (benchmark_move_atr or 0.0))
    return moves


def build_impact_calibration_report(
    observations: Sequence[ImpactObservation],
    *,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> ImpactCalibrationReport:
    if min_sample_size < 1:
        raise ValueError("min_sample_size must be positive")
    usable = [obs for obs in observations if obs.magnitude in MAGNITUDES]

    by_magnitude = _group_buckets(
        usable, key=lambda obs: obs.magnitude, order=MAGNITUDES, min_sample_size=min_sample_size
    )
    by_event_type = _group_buckets(
        usable,
        key=lambda obs: f"{obs.event_type or 'unknown'} / {obs.magnitude}",
        order=None,
        min_sample_size=min_sample_size,
    )
    by_horizon = _group_buckets(
        usable,
        key=lambda obs: f"{obs.magnitude} / {obs.impact_horizon or 'unknown'}",
        order=None,
        min_sample_size=min_sample_size,
    )
    return ImpactCalibrationReport(
        observation_count=len(usable),
        min_sample_size=min_sample_size,
        by_magnitude=by_magnitude,
        by_event_type_magnitude=by_event_type,
        by_magnitude_horizon=by_horizon,
        monotonic_by_horizon=_monotonicity(by_magnitude),
    )


def format_impact_calibration_markdown(report: ImpactCalibrationReport) -> str:
    lines = [
        "# Impact Calibration Report",
        "",
        f"Observations (analyses with a magnitude and buy/sell direction): {report.observation_count}",
        f"Minimum sample size per bucket: {report.min_sample_size}",
        "Moves are direction-aligned and expressed in ATR_20d units; positive = the",
        "market moved the way the analysis predicted. The intraday horizon is",
        "approximated by the first session close (daily bars only).",
        "",
    ]
    for sessions, monotonic in sorted(report.monotonic_by_horizon.items()):
        verdict = "yes" if monotonic else ("no" if monotonic is not None else "insufficient data")
        lines.append(
            f"Median move increases low -> medium -> high at {sessions} session(s): {verdict}"
        )
    lines.append("")
    lines.extend(_bucket_table("By magnitude", report.by_magnitude))
    lines.extend(_bucket_table("By event type x magnitude", report.by_event_type_magnitude))
    lines.extend(_bucket_table("By magnitude x predicted horizon", report.by_magnitude_horizon))
    return "\n".join(lines)


def _baseline_index(ordered: Sequence[DailyBar], published_at: datetime) -> int | None:
    baseline_index = None
    for index, bar in enumerate(ordered):
        if bar.start_at.date() < published_at.date():
            baseline_index = index
        else:
            break
    return baseline_index


def _benchmark_move_atr(
    benchmark_bars: Sequence[DailyBar] | None,
    *,
    published_at: datetime,
    sessions: int,
    instrument_baseline_close: float,
    atr_20d: float,
) -> float | None:
    if benchmark_bars is None:
        return None
    ordered = sorted(benchmark_bars, key=lambda bar: bar.start_at)
    baseline_index = _baseline_index(ordered, published_at)
    if baseline_index is None or baseline_index + sessions >= len(ordered):
        return None
    baseline_close = ordered[baseline_index].close
    if baseline_close <= 0:
        return None
    pct_move = (ordered[baseline_index + sessions].close - baseline_close) / baseline_close
    return pct_move * instrument_baseline_close / atr_20d


def _group_buckets(
    observations: Sequence[ImpactObservation],
    *,
    key,
    order: tuple[str, ...] | None,
    min_sample_size: int,
) -> tuple[ImpactBucket, ...]:
    grouped: dict[str, list[ImpactObservation]] = defaultdict(list)
    for obs in observations:
        grouped[key(obs)].append(obs)
    if order is not None:
        labels = [label for label in order if label in grouped]
    else:
        labels = sorted(grouped)
    return tuple(
        _bucket_metrics(label, grouped[label], min_sample_size=min_sample_size)
        for label in labels
    )


def _bucket_metrics(
    label: str, observations: list[ImpactObservation], *, min_sample_size: int
) -> ImpactBucket:
    horizons: dict[int, HorizonStats] = {}
    for sessions in HORIZON_SESSIONS:
        moves = [
            move
            for obs in observations
            if (move := obs.moves_atr.get(sessions)) is not None
        ]
        horizons[sessions] = _horizon_stats(moves)
    return ImpactBucket(
        label=label,
        observation_count=len(observations),
        sample_too_small=len(observations) < min_sample_size,
        horizons=horizons,
    )


def _horizon_stats(moves: list[float]) -> HorizonStats:
    if not moves:
        return HorizonStats(
            observation_count=0,
            hit_rate=None,
            mean_move_atr=None,
            median_move_atr=None,
            p25_move_atr=None,
            p75_move_atr=None,
        )
    if len(moves) >= 2:
        quartiles = quantiles(moves, n=4, method="inclusive")
        p25, p75 = quartiles[0], quartiles[2]
    else:
        p25 = p75 = moves[0]
    return HorizonStats(
        observation_count=len(moves),
        hit_rate=sum(1 for move in moves if move > 0) / len(moves),
        mean_move_atr=mean(moves),
        median_move_atr=median(moves),
        p25_move_atr=p25,
        p75_move_atr=p75,
    )


def _monotonicity(by_magnitude: tuple[ImpactBucket, ...]) -> dict[int, bool | None]:
    buckets = {bucket.label: bucket for bucket in by_magnitude}
    result: dict[int, bool | None] = {}
    for sessions in HORIZON_SESSIONS:
        medians = []
        for magnitude in MAGNITUDES:
            bucket = buckets.get(magnitude)
            stats = bucket.horizons.get(sessions) if bucket else None
            medians.append(stats.median_move_atr if stats else None)
        if any(value is None for value in medians):
            result[sessions] = None
        else:
            result[sessions] = all(a < b for a, b in zip(medians, medians[1:]))
    return result


def _bucket_table(title: str, buckets: tuple[ImpactBucket, ...]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Bucket | N | Hit rate 1s | Median 1s | Mean 1s | p25/p75 1s | Hit rate 5s | Median 5s | Mean 5s | p25/p75 5s | Sample warning |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for bucket in buckets:
        one = bucket.horizons.get(1)
        five = bucket.horizons.get(5)
        lines.append(
            "| "
            + " | ".join(
                [
                    bucket.label,
                    str(bucket.observation_count),
                    _fmt_pct(one.hit_rate if one else None),
                    _fmt_atr(one.median_move_atr if one else None),
                    _fmt_atr(one.mean_move_atr if one else None),
                    _fmt_range(one),
                    _fmt_pct(five.hit_rate if five else None),
                    _fmt_atr(five.median_move_atr if five else None),
                    _fmt_atr(five.mean_move_atr if five else None),
                    _fmt_range(five),
                    "insufficient sample" if bucket.sample_too_small else "",
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def _bucket_dict(bucket: ImpactBucket) -> dict[str, Any]:
    return {
        "label": bucket.label,
        "observation_count": bucket.observation_count,
        "sample_too_small": bucket.sample_too_small,
        "horizons": {
            str(sessions): {
                "observation_count": stats.observation_count,
                "hit_rate": stats.hit_rate,
                "mean_move_atr": stats.mean_move_atr,
                "median_move_atr": stats.median_move_atr,
                "p25_move_atr": stats.p25_move_atr,
                "p75_move_atr": stats.p75_move_atr,
            }
            for sessions, stats in bucket.horizons.items()
        },
    }


def _fmt_pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else ""


def _fmt_atr(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else ""


def _fmt_range(stats: HorizonStats | None) -> str:
    if stats is None or stats.p25_move_atr is None or stats.p75_move_atr is None:
        return ""
    return f"{stats.p25_move_atr:+.2f}/{stats.p75_move_atr:+.2f}"
