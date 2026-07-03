import dataclasses

import pytest

from src.product_components.trade_executor.settings import (
    ModePortMismatchError,
    TradeExecutorSettings,
)


def _clear(monkeypatch) -> None:
    for key in ("TRADE_EXECUTOR_TRADING_MODE", "IBKR_PORT"):
        monkeypatch.delenv(key, raising=False)


def _settings(monkeypatch, *, mode: str, port: int) -> TradeExecutorSettings:
    _clear(monkeypatch)
    monkeypatch.setenv("TRADE_EXECUTOR_TRADING_MODE", mode)
    monkeypatch.setenv("IBKR_PORT", str(port))
    return TradeExecutorSettings.from_env()


def test_paper_with_paper_port_ok(monkeypatch) -> None:
    _settings(monkeypatch, mode="paper", port=7497).validate_mode_port_agreement()
    _settings(monkeypatch, mode="paper", port=4002).validate_mode_port_agreement()


def test_live_with_live_port_ok(monkeypatch) -> None:
    _settings(monkeypatch, mode="live", port=7496).validate_mode_port_agreement()
    _settings(monkeypatch, mode="live", port=4001).validate_mode_port_agreement()


def test_paper_mode_live_port_refused(monkeypatch) -> None:
    with pytest.raises(ModePortMismatchError):
        _settings(monkeypatch, mode="paper", port=7496).validate_mode_port_agreement()


def test_live_mode_paper_port_refused(monkeypatch) -> None:
    with pytest.raises(ModePortMismatchError):
        _settings(monkeypatch, mode="live", port=7497).validate_mode_port_agreement()


def test_unknown_port_refused(monkeypatch) -> None:
    with pytest.raises(ModePortMismatchError):
        _settings(monkeypatch, mode="paper", port=9999).validate_mode_port_agreement()


def test_bad_mode_refused(monkeypatch) -> None:
    settings = _settings(monkeypatch, mode="paper", port=7497)
    bad = dataclasses.replace(settings, trading_mode="sim")
    with pytest.raises(ModePortMismatchError):
        bad.validate_mode_port_agreement()
