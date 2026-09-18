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
    DecisionComparison,
    DecisionExplanation,
    DecisionOperand,
    DecisionReason,
    DecisionRelatedRecord,
    DecisionStage,
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
    related_position: DecisionRelatedRecord | None = None,
) -> GateOutcome:
    """Deterministic admission gate (behavior.md §3.2).

    Returns the first failing reason in the documented order, or ``admit()``.
    ``duplicate_card`` is not checked here — it is enforced by the UNIQUE
    constraint on t_trade_decisions.thesis_card_id at persistence time.
    """
    if card.direction == TradeDirection.HOLD:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.DIRECTION_HOLD,
            check_id="admission.actionable_direction",
            observed=("direction", card.direction, None),
            operator="in",
            expected=("actionable_directions", ["buy", "sell"], None),
        )
    if now > card.expires_at:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.CARD_EXPIRED,
            check_id="admission.not_expired",
            observed=("decision_time", now, "timestamp"),
            operator="<=",
            expected=("expires_at", card.expires_at, "timestamp"),
            inputs=(("created_at", card.created_at, "timestamp"),),
            expires_at=card.expires_at.isoformat(),
        )
    if card.confidence < min_confidence:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.BELOW_MIN_CONFIDENCE,
            check_id="admission.minimum_confidence",
            observed=("confidence", card.confidence, "ratio"),
            operator=">=",
            expected=("minimum_confidence", min_confidence, "ratio"),
            confidence=card.confidence,
            min_confidence=min_confidence,
        )
    if not in_watchlist:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.NOT_IN_WATCHLIST,
            check_id="admission.watchlist_membership",
            observed=("in_watchlist", in_watchlist, "boolean"),
            operator="=",
            expected=("required_membership", True, "boolean"),
        )
    if has_open_or_working_position:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.POSITION_EXISTS,
            check_id="admission.no_existing_position",
            observed=(
                "has_open_or_working_position",
                has_open_or_working_position,
                "boolean",
            ),
            operator="=",
            expected=("required_existing_position", False, "boolean"),
            related_records=(related_position,) if related_position is not None else (),
        )
    if review_state != "approved":
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.REVIEW_NOT_APPROVED,
            check_id="admission.review_state",
            observed=("review_state", review_state, "state"),
            operator="=",
            expected=("required_review_state", "approved", "state"),
            review_state=review_state,
        )
    if card.time_horizon not in horizon_map:
        return _reject(
            stage=DecisionStage.ADMISSION,
            reason=DecisionReason.HORIZON_UNMAPPED,
            check_id="admission.horizon_mapping",
            observed=("time_horizon", card.time_horizon, "horizon"),
            operator="in",
            expected=("mapped_horizons", sorted(horizon_map), "horizon_list"),
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
    atr_20d: float | None = None,
    atr_stop_mult: float | None = None,
) -> SizedOrder:
    """Risk-based sizing (behavior.md §3.5).

    ``qty = floor(max_loss_usd / |entry - stop|)`` clamped so notional does not
    exceed ``max_position_size`` or the remaining portfolio headroom. A returned
    quantity below one share signals rejection (``size_below_one_share``).
    """
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or entry <= 0:
        binding_constraint = (
            "valid_stop_distance" if stop_distance <= 0 else "valid_entry_price"
        )
        return SizedOrder(
            quantity=0,
            notional=0.0,
            binding_constraint=binding_constraint,
            explanation=_sizing_rejection_explanation(
                max_loss_usd=max_loss_usd,
                entry=entry,
                stop=stop,
                stop_distance=stop_distance,
                max_position_size=max_position_size,
                portfolio_headroom=portfolio_headroom,
                atr_20d=atr_20d,
                atr_stop_mult=atr_stop_mult,
                risk_budget_quantity=None,
                position_cap_quantity=None,
                portfolio_headroom_quantity=None,
                binding_constraint=binding_constraint,
                binding_constraints=(binding_constraint,),
            ),
        )

    risk_budget_quantity = math.floor(max_loss_usd / stop_distance)
    position_cap_quantity = math.floor(max_position_size / entry)
    portfolio_headroom_quantity = math.floor(max(portfolio_headroom, 0.0) / entry)
    independent_quantities = {
        "risk_budget": risk_budget_quantity,
        "per_position_notional": position_cap_quantity,
        "portfolio_headroom": portfolio_headroom_quantity,
    }
    raw_quantity = min(independent_quantities.values())
    binding_constraints = tuple(
        name for name, quantity in independent_quantities.items() if quantity == raw_quantity
    )
    binding_constraint = binding_constraints[0]
    if raw_quantity < 1:
        return SizedOrder(
            quantity=0,
            notional=0.0,
            risk_budget_quantity=risk_budget_quantity,
            position_cap_quantity=position_cap_quantity,
            portfolio_headroom_quantity=portfolio_headroom_quantity,
            binding_constraint=binding_constraint,
            explanation=_sizing_rejection_explanation(
                max_loss_usd=max_loss_usd,
                entry=entry,
                stop=stop,
                stop_distance=stop_distance,
                max_position_size=max_position_size,
                portfolio_headroom=portfolio_headroom,
                atr_20d=atr_20d,
                atr_stop_mult=atr_stop_mult,
                risk_budget_quantity=risk_budget_quantity,
                position_cap_quantity=position_cap_quantity,
                portfolio_headroom_quantity=portfolio_headroom_quantity,
                binding_constraint=binding_constraint,
                binding_constraints=binding_constraints,
            ),
        )
    return SizedOrder(
        quantity=raw_quantity,
        notional=raw_quantity * entry,
        risk_budget_quantity=risk_budget_quantity,
        position_cap_quantity=position_cap_quantity,
        portfolio_headroom_quantity=portfolio_headroom_quantity,
        binding_constraint=binding_constraint,
    )


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
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.DAILY_LOSS_HALT,
            check_id="portfolio_risk.daily_loss_halt_latched",
            observed=("halt_latched", True, "boolean"),
            operator="=",
            expected=("required_halt_latched", False, "boolean"),
            inputs=(
                ("realized_pnl", daily.realized_pnl, "usd"),
                ("unrealized_pnl", daily.unrealized_pnl, "usd"),
                ("daily_loss_limit", daily_loss_limit, "usd"),
            ),
            latched=True,
        )
    day_pnl = daily.realized_pnl + daily.unrealized_pnl
    if day_pnl <= -daily_loss_limit:
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.DAILY_LOSS_HALT,
            check_id="portfolio_risk.daily_loss_limit",
            observed=("combined_daily_pnl", day_pnl, "usd"),
            operator=">",
            expected=("minimum_daily_pnl", -daily_loss_limit, "usd"),
            inputs=(
                ("realized_pnl", daily.realized_pnl, "usd"),
                ("unrealized_pnl", daily.unrealized_pnl, "usd"),
                ("daily_loss_limit", daily_loss_limit, "usd"),
                ("halt_was_latched", daily.halted, "boolean"),
            ),
            derived=(("combined_daily_pnl", day_pnl, "usd"),),
            day_pnl=day_pnl,
            daily_loss_limit=daily_loss_limit,
            halt_triggered=True,
        )

    if daily.trades_count >= max_daily_trades:
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.MAX_DAILY_TRADES_REACHED,
            check_id="portfolio_risk.max_daily_trades",
            observed=("daily_trade_count", daily.trades_count, "trades"),
            operator="<",
            expected=("maximum_daily_trades", max_daily_trades, "trades"),
            trades_count=daily.trades_count,
            max_daily_trades=max_daily_trades,
        )

    if portfolio.open_and_working_count >= max_positions:
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            check_id="portfolio_risk.max_positions",
            observed=(
                "open_and_working_position_count",
                portfolio.open_and_working_count,
                "positions",
            ),
            operator="<",
            expected=("maximum_positions", max_positions, "positions"),
            binding_constraint="max_positions",
            cap="max_positions",
            open_and_working_count=portfolio.open_and_working_count,
            max_positions=max_positions,
        )

    if portfolio.deployed_capital + new_notional > max_portfolio_exposure:
        combined_exposure = portfolio.deployed_capital + new_notional
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            check_id="portfolio_risk.max_portfolio_exposure",
            observed=("portfolio_exposure_after_order", combined_exposure, "usd"),
            operator="<=",
            expected=(
                "maximum_portfolio_exposure",
                max_portfolio_exposure,
                "usd",
            ),
            inputs=(
                ("deployed_capital", portfolio.deployed_capital, "usd"),
                ("proposed_notional", new_notional, "usd"),
            ),
            derived=(("portfolio_exposure_after_order", combined_exposure, "usd"),),
            binding_constraint="max_portfolio_exposure",
            cap="max_portfolio_exposure",
            deployed_capital=portfolio.deployed_capital,
            new_notional=new_notional,
            max_portfolio_exposure=max_portfolio_exposure,
        )

    # Best-effort per-sector cap; skipped when sector is unknown.
    if portfolio.sector_known and portfolio.sector_exposure + new_notional > max_sector_exposure:
        combined_sector_exposure = portfolio.sector_exposure + new_notional
        return _reject(
            stage=DecisionStage.PORTFOLIO_RISK,
            reason=DecisionReason.PORTFOLIO_CAP_EXCEEDED,
            check_id="portfolio_risk.max_sector_exposure",
            observed=("sector_exposure_after_order", combined_sector_exposure, "usd"),
            operator="<=",
            expected=("maximum_sector_exposure", max_sector_exposure, "usd"),
            inputs=(
                ("sector_exposure", portfolio.sector_exposure, "usd"),
                ("proposed_notional", new_notional, "usd"),
                ("sector_known", portfolio.sector_known, "boolean"),
            ),
            derived=(("sector_exposure_after_order", combined_sector_exposure, "usd"),),
            binding_constraint="max_sector_exposure",
            cap="max_sector_exposure",
            sector_exposure=portfolio.sector_exposure,
            new_notional=new_notional,
            max_sector_exposure=max_sector_exposure,
        )

    return GateOutcome.admit(new_notional=new_notional)


def atr_unavailable_outcome(
    *,
    atr_20d: float | None,
    source_status: str | None = None,
    as_of: datetime | None = None,
    bars_fetched_at: datetime | None = None,
    required_lookback_bars: int = 20,
    required_source_bars: int = 21,
    available_bars: int | None = None,
    availability_status: str | None = None,
    failure_category: str | None = None,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
) -> GateOutcome:
    """Build the safe, shared explanation for unavailable ATR market data.

    The contract accepts only bounded availability facts. In particular it
    deliberately excludes provider error messages and metadata, which can
    contain request data or credentials.
    """
    inputs: list[tuple[str, object, str | None]] = [
        ("required_metric", "atr_20d", "metric"),
        ("required_lookback_bars", required_lookback_bars, "daily_bars"),
        ("required_source_bars", required_source_bars, "daily_bars"),
    ]
    for name, value, unit in (
        ("source_status", source_status, "status"),
        ("context_as_of", as_of, "timestamp"),
        ("bars_fetched_at", bars_fetched_at, "timestamp"),
        ("availability_status", availability_status, "status"),
        ("failure_category", failure_category, "category"),
        ("coverage_start", coverage_start, "timestamp"),
        ("coverage_end", coverage_end, "timestamp"),
        ("available_bars", available_bars, "daily_bars"),
    ):
        if value is not None:
            inputs.append((name, value, unit))
    return _reject(
        stage=DecisionStage.MARKET_DATA,
        reason=DecisionReason.ATR_UNAVAILABLE,
        check_id="market_data.atr_20d_available",
        observed=("atr_20d", atr_20d, "price"),
        operator=">",
        expected=("minimum_atr_20d", 0.0, "price"),
        inputs=tuple(inputs),
    )


def _sizing_rejection_explanation(
    *,
    max_loss_usd: float,
    entry: float,
    stop: float,
    stop_distance: float,
    max_position_size: float,
    portfolio_headroom: float,
    atr_20d: float | None,
    atr_stop_mult: float | None,
    risk_budget_quantity: int | None,
    position_cap_quantity: int | None,
    portfolio_headroom_quantity: int | None,
    binding_constraint: str,
    binding_constraints: tuple[str, ...],
) -> DecisionExplanation:
    inputs = [
        DecisionOperand("risk_budget", max_loss_usd, "usd"),
        DecisionOperand("entry_price", entry, "usd_per_share"),
        DecisionOperand("stop_price", stop, "usd_per_share"),
        DecisionOperand("per_position_cap", max_position_size, "usd"),
        DecisionOperand("portfolio_headroom", portfolio_headroom, "usd"),
    ]
    if atr_20d is not None:
        inputs.append(DecisionOperand("atr_20d", atr_20d, "usd_per_share"))
    if atr_stop_mult is not None:
        inputs.append(DecisionOperand("atr_stop_multiplier", atr_stop_mult, "multiple"))
    return DecisionExplanation(
        stage=DecisionStage.SIZING,
        reason=DecisionReason.SIZE_BELOW_ONE_SHARE,
        check_id="sizing.minimum_quantity",
        comparison=DecisionComparison(
            observed=DecisionOperand("final_quantity", 0, "shares"),
            operator=">=",
            expected=DecisionOperand("minimum_quantity", 1, "shares"),
        ),
        inputs=tuple(inputs),
        derived_values=(
            DecisionOperand("stop_distance", stop_distance, "usd_per_share"),
            DecisionOperand("risk_budget_quantity", risk_budget_quantity, "shares"),
            DecisionOperand("position_cap_quantity", position_cap_quantity, "shares"),
            DecisionOperand(
                "portfolio_headroom_quantity",
                portfolio_headroom_quantity,
                "shares",
            ),
            DecisionOperand("final_quantity", 0, "shares"),
            DecisionOperand("final_notional", 0.0, "usd"),
            DecisionOperand("binding_constraints", binding_constraints, "constraint_ids"),
        ),
        binding_constraint=binding_constraint,
    )


def _reject(
    *,
    stage: DecisionStage,
    reason: DecisionReason,
    check_id: str,
    observed: tuple[str, object, str | None],
    operator: str,
    expected: tuple[str, object, str | None],
    inputs: tuple[tuple[str, object, str | None], ...] = (),
    derived: tuple[tuple[str, object, str | None], ...] = (),
    binding_constraint: str | None = None,
    related_records: tuple[DecisionRelatedRecord, ...] = (),
    **legacy_details,
) -> GateOutcome:
    explanation = DecisionExplanation(
        stage=stage,
        reason=reason,
        check_id=check_id,
        comparison=DecisionComparison(
            observed=DecisionOperand(*observed),
            operator=operator,
            expected=DecisionOperand(*expected),
        ),
        inputs=tuple(DecisionOperand(*value) for value in inputs),
        derived_values=tuple(DecisionOperand(*value) for value in derived),
        binding_constraint=binding_constraint,
        related_records=related_records,
    )
    return GateOutcome.reject(reason, explanation=explanation, **legacy_details)
