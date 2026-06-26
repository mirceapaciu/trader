"""Pure money/return math helpers for backtest performance metrics."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence


def round_half_up(value: float, ndigits: int) -> float:
    """Round ``value`` to ``ndigits`` decimals using round-half-up."""

    quantum = Decimal(1).scaleb(-ndigits)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Return the linear-interpolation percentile (``pct`` in [0, 100]).

    ``None`` if ``values`` is empty.
    """

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def win_rate(winning_trades: int, closed_trades: int) -> float | None:
    """Fraction of closed trades that won; ``None`` if no closed trades."""

    if closed_trades == 0:
        return None
    return winning_trades / closed_trades


def profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    """Gross profit divided by gross loss magnitude; ``None`` if loss is 0."""

    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def expectancy(net_pnl: float, closed_trades: int) -> float | None:
    """Average net PnL per closed trade; ``None`` if no closed trades."""

    if closed_trades == 0:
        return None
    return net_pnl / closed_trades


def sharpe_ratio(
    period_returns: Sequence[float], risk_free_per_period: float = 0.0
) -> float | None:
    """Non-annualized Sharpe ratio over per-period returns.

    Uses population standard deviation of excess returns. ``None`` if there
    are fewer than two returns or the stdev is zero.
    """

    if len(period_returns) < 2:
        return None
    excess = [r - risk_free_per_period for r in period_returns]
    mean = sum(excess) / len(excess)
    variance = sum((x - mean) ** 2 for x in excess) / len(excess)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return None
    return mean / stdev


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a non-negative fraction of the peak.

    ``0.0`` if fewer than two points or the curve never declines.
    """

    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def max_drawdown_with_duration(
    points: Sequence[tuple[datetime, float]],
) -> tuple[float, float]:
    """Return ``(fraction, duration_seconds)`` for the maximum drawdown.

    Duration runs from the running-peak timestamp to the trough timestamp of
    the maximum drawdown. ``(0.0, 0.0)`` if there is no drawdown.
    """

    if len(points) < 2:
        return (0.0, 0.0)
    peak_ts, peak_value = points[0]
    worst_fraction = 0.0
    worst_duration = 0.0
    for ts, value in points:
        if value > peak_value:
            peak_value = value
            peak_ts = ts
        if peak_value > 0:
            drawdown = (peak_value - value) / peak_value
            if drawdown > worst_fraction:
                worst_fraction = drawdown
                worst_duration = (ts - peak_ts).total_seconds()
    return (worst_fraction, worst_duration)
