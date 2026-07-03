from src.product_components.trade_executor.models import DecisionReason
from src.product_components.trade_executor.pipeline import (
    DailyRiskState,
    PortfolioState,
    evaluate_risk_gate,
)


def _portfolio(**overrides) -> PortfolioState:
    base = dict(
        open_and_working_count=0,
        deployed_capital=0.0,
        sector_exposure=0.0,
        sector_known=False,
    )
    base.update(overrides)
    return PortfolioState(**base)


def _daily(**overrides) -> DailyRiskState:
    base = dict(realized_pnl=0.0, unrealized_pnl=0.0, trades_count=0, halted=False)
    base.update(overrides)
    return DailyRiskState(**base)


def _gate(**overrides):
    kwargs = dict(
        new_quantity=10,
        entry=100.0,
        portfolio=_portfolio(),
        daily=_daily(),
        max_positions=5,
        max_portfolio_exposure=5000.0,
        max_sector_exposure=2500.0,
        daily_loss_limit=200.0,
        max_daily_trades=10,
    )
    kwargs.update(overrides)
    return evaluate_risk_gate(**kwargs)


def test_passes_clean() -> None:
    outcome = _gate()
    assert outcome.passed is True
    assert outcome.details["new_notional"] == 1000.0


def test_max_positions_blocks() -> None:
    outcome = _gate(portfolio=_portfolio(open_and_working_count=5))
    assert outcome.reason == DecisionReason.PORTFOLIO_CAP_EXCEEDED
    assert outcome.details["cap"] == "max_positions"


def test_portfolio_exposure_blocks() -> None:
    outcome = _gate(portfolio=_portfolio(deployed_capital=4500.0))
    assert outcome.reason == DecisionReason.PORTFOLIO_CAP_EXCEEDED
    assert outcome.details["cap"] == "max_portfolio_exposure"


def test_sector_cap_blocks_when_known() -> None:
    outcome = _gate(portfolio=_portfolio(sector_known=True, sector_exposure=2000.0))
    assert outcome.reason == DecisionReason.PORTFOLIO_CAP_EXCEEDED
    assert outcome.details["cap"] == "max_sector_exposure"


def test_sector_cap_skipped_when_unknown() -> None:
    # Same exposure but sector unknown -> not blocked.
    outcome = _gate(portfolio=_portfolio(sector_known=False, sector_exposure=2000.0))
    assert outcome.passed is True


def test_max_daily_trades_blocks() -> None:
    outcome = _gate(daily=_daily(trades_count=10))
    assert outcome.reason == DecisionReason.MAX_DAILY_TRADES_REACHED


def test_daily_loss_trips_kill_switch() -> None:
    outcome = _gate(daily=_daily(realized_pnl=-150.0, unrealized_pnl=-60.0))
    assert outcome.reason == DecisionReason.DAILY_LOSS_HALT
    assert outcome.details.get("halt_triggered") is True


def test_latched_halt_blocks_even_when_pnl_recovered() -> None:
    # PnL is positive now, but the halt latched earlier in the day.
    outcome = _gate(daily=_daily(realized_pnl=500.0, halted=True))
    assert outcome.reason == DecisionReason.DAILY_LOSS_HALT
    assert outcome.details.get("latched") is True
