from datetime import datetime, timezone

from src.product_components.trade_executor.pipeline import (
    atr_unavailable_outcome,
    size_position,
)


def test_risk_based_quantity() -> None:
    # stop distance = 3 ; floor(120/3) = 40 ; notional cap big enough
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=100_000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 40
    assert order.notional == 40 * 100.0
    assert order.risk_budget_quantity == 40
    assert order.position_cap_quantity == 1000
    assert order.portfolio_headroom_quantity == 1000
    assert order.binding_constraint == "risk_budget"


def test_clamped_by_max_position_size() -> None:
    # risk allows 40, but max_position_size=1000 / entry 100 = 10 shares
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=1000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 10
    assert order.binding_constraint == "per_position_notional"


def test_clamped_by_headroom() -> None:
    # headroom 500 / 100 = 5 shares
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=100_000.0, portfolio_headroom=500.0,
    )
    assert order.quantity == 5
    assert order.binding_constraint == "portfolio_headroom"


def test_below_one_share_rejected() -> None:
    # stop distance huge -> floor(120/200) = 0
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=-100.0,
        max_position_size=100_000.0, portfolio_headroom=100_000.0,
        atr_20d=100.0, atr_stop_mult=2.0,
    )
    assert order.quantity == 0
    assert order.risk_budget_quantity == 0
    assert order.position_cap_quantity == 1000
    assert order.portfolio_headroom_quantity == 1000
    assert order.binding_constraint == "risk_budget"
    detail = order.explanation.as_dict()
    assert detail["schema_version"] == 1
    assert detail["stage"] == "sizing"
    assert detail["reason"] == "size_below_one_share"
    assert detail["inputs"]["atr_20d"]["value"] == 100.0
    assert detail["inputs"]["atr_stop_multiplier"]["value"] == 2.0
    assert detail["derived_values"]["risk_budget_quantity"]["value"] == 0


def test_below_one_share_when_position_cap_binds() -> None:
    order = size_position(
        max_loss_usd=120.0,
        entry=100.0,
        stop=97.0,
        max_position_size=99.0,
        portfolio_headroom=100_000.0,
    )
    assert order.quantity == 0
    assert order.risk_budget_quantity == 40
    assert order.position_cap_quantity == 0
    assert order.binding_constraint == "per_position_notional"


def test_below_one_share_when_portfolio_headroom_binds() -> None:
    order = size_position(
        max_loss_usd=120.0,
        entry=100.0,
        stop=97.0,
        max_position_size=100_000.0,
        portfolio_headroom=99.0,
    )
    assert order.quantity == 0
    assert order.portfolio_headroom_quantity == 0
    assert order.binding_constraint == "portfolio_headroom"


def test_zero_stop_distance_rejected() -> None:
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=100.0,
        max_position_size=100_000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 0


def test_negative_headroom_rejected() -> None:
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=100_000.0, portfolio_headroom=-50.0,
    )
    assert order.quantity == 0


def test_atr_unavailable_has_safe_bounded_coverage_context() -> None:
    as_of = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)
    outcome = atr_unavailable_outcome(
        atr_20d=None,
        source_status="missing",
        as_of=as_of,
        required_source_bars=21,
        available_bars=12,
        availability_status="insufficient_coverage",
        failure_category="insufficient_history",
        coverage_start=datetime(2026, 8, 28, tzinfo=timezone.utc),
        coverage_end=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    detail = outcome.explanation.as_dict()
    assert detail["stage"] == "market_data"
    assert detail["reason"] == "atr_unavailable"
    assert detail["comparison"]["observed"]["value"] is None
    assert detail["inputs"]["required_source_bars"]["value"] == 21
    assert detail["inputs"]["available_bars"]["value"] == 12
    assert detail["inputs"]["failure_category"]["value"] == "insufficient_history"
    assert "error_message" not in detail["inputs"]
