"""Reusable simulation harness primitives for the Backtester component.

This core module is domain-free and must not import from
``src/product_components``.
"""

from __future__ import annotations

from src.core_components.backtest_engine.bars import Bar, BarSeries
from src.core_components.backtest_engine.fills import CommissionModel, apply_slippage
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

__all__ = [
    "Bar",
    "BarSeries",
    "CommissionModel",
    "apply_slippage",
    "round_half_up",
    "percentile",
    "win_rate",
    "profit_factor",
    "expectancy",
    "sharpe_ratio",
    "max_drawdown",
    "max_drawdown_with_duration",
]
