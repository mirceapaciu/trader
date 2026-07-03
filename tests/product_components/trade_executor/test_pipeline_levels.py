import pytest

from src.product_components.trade_executor.pipeline import construct_levels, entry_limit_price


def test_buy_levels() -> None:
    levels = construct_levels(
        direction="buy", entry=100.0, atr_20d=2.0, atr_stop_mult=1.5, take_profit_r=2.0
    )
    # stop = 100 - 1.5*2 = 97 ; target = 100 + 2*(100-97) = 106
    assert levels.stop == pytest.approx(97.0)
    assert levels.target == pytest.approx(106.0)
    assert levels.stop_distance == pytest.approx(3.0)


def test_sell_levels() -> None:
    levels = construct_levels(
        direction="sell", entry=100.0, atr_20d=2.0, atr_stop_mult=1.5, take_profit_r=2.0
    )
    # stop = 100 + 3 = 103 ; target = 100 - 2*(103-100) = 94
    assert levels.stop == pytest.approx(103.0)
    assert levels.target == pytest.approx(94.0)
    assert levels.stop_distance == pytest.approx(3.0)


def test_entry_limit_buy_adds_slippage() -> None:
    # 5 bps above ask: 200 * 1.0005 = 200.1
    price = entry_limit_price(direction="buy", bid=199.9, ask=200.0, slippage_bps=5.0)
    assert price == pytest.approx(200.1)


def test_entry_limit_sell_subtracts_slippage() -> None:
    price = entry_limit_price(direction="sell", bid=200.0, ask=200.1, slippage_bps=5.0)
    assert price == pytest.approx(199.9)
