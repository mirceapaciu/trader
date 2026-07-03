from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from src.product_components.trade_executor.main import _configure_logging, _verify_configuration
from src.product_components.trade_executor.settings import TradeExecutorSettings


def _settings() -> TradeExecutorSettings:
    return TradeExecutorSettings.from_env()


def test_verify_configuration_accepts_paper_paper() -> None:
    _verify_configuration(replace(_settings(), trading_mode="paper", ibkr_port=7497))


def test_verify_configuration_exits_on_mode_port_mismatch() -> None:
    with pytest.raises(SystemExit):
        _verify_configuration(replace(_settings(), trading_mode="paper", ibkr_port=7496))


def test_verify_configuration_exits_on_unknown_port() -> None:
    with pytest.raises(SystemExit):
        _verify_configuration(replace(_settings(), trading_mode="paper", ibkr_port=9999))


def test_configure_logging_writes_to_configured_log_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "trade-executor.log"
    settings = replace(_settings(), log_file=str(log_file), log_level="INFO")
    try:
        _configure_logging(settings, tmp_path)
        logging.getLogger("trade_executor").info("trade executor log smoke test")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "trade executor log smoke test" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.basicConfig(handlers=[], force=True)
