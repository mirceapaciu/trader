from src.product_components.trade_executor.pipeline import size_position


def test_risk_based_quantity() -> None:
    # stop distance = 3 ; floor(120/3) = 40 ; notional cap big enough
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=100_000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 40
    assert order.notional == 40 * 100.0


def test_clamped_by_max_position_size() -> None:
    # risk allows 40, but max_position_size=1000 / entry 100 = 10 shares
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=1000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 10


def test_clamped_by_headroom() -> None:
    # headroom 500 / 100 = 5 shares
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=97.0,
        max_position_size=100_000.0, portfolio_headroom=500.0,
    )
    assert order.quantity == 5


def test_below_one_share_rejected() -> None:
    # stop distance huge -> floor(120/200) = 0
    order = size_position(
        max_loss_usd=120.0, entry=100.0, stop=-100.0,
        max_position_size=100_000.0, portfolio_headroom=100_000.0,
    )
    assert order.quantity == 0


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
