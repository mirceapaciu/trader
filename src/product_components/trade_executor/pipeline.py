"""Pure, side-effect-free decision logic for the TradeExecutor.

Everything here takes plain values / frozen dataclasses and returns them; there
is no database, Redis, or broker dependency, so the admission gate, level math,
sizing, and risk gate are unit-testable with zero fakes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .models import (
    DecisionReason,
    GateOutcome,
    SizedOrder,
    ThesisCard,
    TradeDirection,
    TradeLevels,
)


@dataclass(frozen=True)
class PortfolioState:
    """Snapshot of live portfolio exposure used by the risk gate.

    ``open_and_working_count`` and ``deployed_capital`` include both filled open
    positions and the reserved notional of submitted-but-unfilled entry orders,
    so a batch of concurrently admitted cards cannot collectively breach a cap.
    """

    open_and_working_count: int
    deployed_capital: float
    sector_exposure: float
    sector_known: bool


@dataclass(frozen=True)
class DailyRiskState:
    realized_pnl: float
    unrealized_pnl: float
    trades_count: int
    halted: bool


def evaluate_admission_gate(
    *,
    card: ThesisCard,
    now: datetime,
    min_confidence: float,
    in_watchlist: bool,
    review_state: str | None,
    has_open_or_working_position: bool,
    horizon_map: dict[str, int],
) -> GateOutcome:
    """Deterministic admission gate (behavior.md §3.2).

    Returns the first failing reason in the documented order, or ``admit()``.
    ``duplicate_card`` is not checked here — it is enforced by the UNIQUE
    constraint on t_trade_decisions.thesis_card_id at persistence time.
    """
    if card.direction == TradeDirection.HOLD:
        return GateOutcome.reject(DecisionReason.DIRECTION_HOLD)
    if now > card.expires_at:
        return GateOutcome.reject(
            DecisionReason.CARD_EXPIRED,
            expires_at=card.expires_at.isoformat(),
        )
    if card.confidence < min_confidence:
        return GateOutcome.reject(
            DecisionReason.BELOW_MIN_CONFIDENCE,
            confidence=card.confidence,
            min_confidence=min_confidence,
        )
    if not in_watchlist:
        return GateOutcome.reject(DecisionReason.NOT_IN_WATCHLIST)
    if has_open_or_working_position:
        return GateOutcome.reject(DecisionReason.POSITION_EXISTS)
    if review_state != "approved":
        return GateOutcome.reject(
            DecisionReason.REVIEW_NOT_APPROVED,
            review_state=review_state,
        )
    if card.time_horizon not in horizon_map:
        return GateOutcome.reject(
            DecisionReason.HORIZON_UNMAPPED,
            time_horizon=card.time_horizon,
        )
    return GateOutcome.admit()


def construct_levels(
    *,
    direction: str,
    entry: float,
    atr_20d: float,
    atr_stop_mult: float,
    take_profit_r: float,
) -> TradeLevels:
    """ATR bracket + R-multiple level construction (behavior.md §3.4)."""
    stop_distance = atr_stop_mult * atr_20d
    if direction == TradeDirection.BUY:
        stop = entry - stop_distance
        target = entry + take_profit_r * (entry - stop)
    else:  # sell / short
        stop = entry + stop_distance
        target = entry - take_profit_r * (stop - entry)
    return TradeLevels(entry=entry, stop=stop, target=target)


def entry_limit_price(
    *,
    direction: str,
    bid: float,
    ask: float,
    slippage_bps: float,
) -> float:
    """Marketable-limit entry price: ask+buffer (buy) / bid-buffer (sell)."""
    if direction == TradeDirection.BUY:
        return ask * (1.0 + slippage_bps / 10_000.0)
    return bid * (1.0 - slippage_bps / 10_000.0)


def size_position(
    *,
    max_loss_usd: float,
    entry: float,
    stop: float,
    max_position_size: float,
    portfolio_headroom: float,
) -> SizedOrder:
    """Risk-based sizing (behavior.md §3.5).

    ``qty = floor(max_loss_usd / |entry - stop|)`` clamped so notional does not
    exceed ``max_position_size`` or the remaining portfolio headroom. A returned
    quantity below one share signals rejection (``size_below_one_share``).
    """
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or entry <= 0:
        return SizedOrder(quantity=0, notional=0.0)

    qty = math.floor(max_loss_usd / stop_distance)
    qty = min(
        qty,
        math.floor(max_position_size / entry),
        math.floor(max(portfolio_headroom, 0.0) / entry),
    )
    if qty < 1:
        return SizedOrder(quantity=0, notional=0.0)
    return SizedOrder(quantity=qty, notional=qty * entry)


def evaluate_risk_gate(
    *,
    new_quantity: int,
    entry: float,
    portfolio: PortfolioState,
    daily: DailyRiskState,
    max_positions: int,
    max_portfolio_exposure: float,
    max_sector_exposure: float,
    daily_loss_limit: float,
    max_daily_trades: int,
) -> GateOutcome:
    """Portfolio guardrails + latching daily-loss kill-switch (behavior.md §3.6).

    When the loss threshold trips, ``details['halt_triggered']`` is set so the
    caller can latch ``t_daily_risk.halted`` for the remainder of the day.
    """
    new_notional = new_quantity * entry

    # Kill-switch: already-latched halt, then a fresh trip on combined day PnL.
    if daily.halted:
        return GateOutcome.reject(DecisionReason.DAILY_LOSS_HALT, latched=True)
    day_pnl = daily.realized_pnl + daily.unrealized_pnl
    if day_pnl <= -daily_loss_limit:
        return GateOutcome.reject(
            DecisionReason.DAILY_LOSS_HALT,
            day_pnl=day_pnl,
            daily_loss_limit=daily_loss_limit,
            halt_triggered=True,
        )

    if daily.trades_count >= max_daily_trades:
        return GateOutcome.reject(
            DecisionReason.MAX_DAILY_TRADES_REACHED,
            trades_count=daily.trades_count,
            max_daily_trades=max_daily_trades,
        )

    if portfolio.open_and_working_count >= max_positions:
        return GateOutcome.reject(
            DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            cap="max_positions",
            open_and_working_count=portfolio.open_and_working_count,
            max_positions=max_positions,
        )

    if portfolio.deployed_capital + new_notional > max_portfolio_exposure:
        return GateOutcome.reject(
            DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            cap="max_portfolio_exposure",
            deployed_capital=portfolio.deployed_capital,
            new_notional=new_notional,
            max_portfolio_exposure=max_portfolio_exposure,
        )

    # Best-effort per-sector cap; skipped when sector is unknown.
    if portfolio.sector_known and portfolio.sector_exposure + new_notional > max_sector_exposure:
        return GateOutcome.reject(
            DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            cap="max_sector_exposure",
            sector_exposure=portfolio.sector_exposure,
            new_notional=new_notional,
            max_sector_exposure=max_sector_exposure,
        )

    return GateOutcome.admit(new_notional=new_notional)
