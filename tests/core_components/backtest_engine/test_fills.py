"""Unit tests for commission and slippage models."""

from __future__ import annotations

import pytest

from src.core_components.backtest_engine.fills import CommissionModel, apply_slippage


def test_per_share_above_floor():
    model = CommissionModel(model="per_share", per_share_usd=0.01, min_usd=1.0)
    # 500 shares * 0.01 = 5.0 > 1.0 floor
    assert model.cost(500, 10.0) == pytest.approx(5.0)


def test_per_share_floor_applied():
    model = CommissionModel(model="per_share", per_share_usd=0.005, min_usd=1.0)
    # 10 shares * 0.005 = 0.05 -> floored to 1.0
    assert model.cost(10, 10.0) == pytest.approx(1.0)


def test_per_share_uses_abs_quantity():
    model = CommissionModel(model="per_share", per_share_usd=0.01, min_usd=0.0)
    assert model.cost(-500, 10.0) == pytest.approx(5.0)


def test_per_share_fractional_quantity():
    model = CommissionModel(model="per_share", per_share_usd=1.0, min_usd=0.0)
    assert model.cost(2.5, 10.0) == pytest.approx(2.5)


def test_flat_model():
    model = CommissionModel(model="flat", flat_usd=3.5)
    assert model.cost(1000, 10.0) == pytest.approx(3.5)
    assert model.cost(1, 10.0) == pytest.approx(3.5)


def test_unknown_model_raises():
    model = CommissionModel(model="mystery")
    with pytest.raises(ValueError):
        model.cost(100, 10.0)


def test_default_model_is_per_share():
    model = CommissionModel()
    assert model.model == "per_share"
    # 100 * 0.005 = 0.5 -> floored to 1.0
    assert model.cost(100, 10.0) == pytest.approx(1.0)


def test_slippage_worse_is_higher():
    # 100 bps = 1%
    assert apply_slippage(100.0, 100.0, worse_is_higher=True) == pytest.approx(101.0)


def test_slippage_worse_is_lower():
    assert apply_slippage(100.0, 100.0, worse_is_higher=False) == pytest.approx(99.0)


def test_slippage_zero_bps_no_change():
    assert apply_slippage(100.0, 0.0, worse_is_higher=True) == pytest.approx(100.0)
    assert apply_slippage(100.0, 0.0, worse_is_higher=False) == pytest.approx(100.0)
