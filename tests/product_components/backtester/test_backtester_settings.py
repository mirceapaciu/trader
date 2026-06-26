from __future__ import annotations

import os
from unittest import mock

from src.product_components.backtester.settings import BacktesterSettings


def test_from_env_defaults():
    with mock.patch.dict(os.environ, {}, clear=True):
        settings = BacktesterSettings.from_env()
    assert settings.db_schema == "backtester"
    assert settings.default_mode == "replay"
    assert settings.default_timing_scenario == "ideal"
    assert settings.delay_buckets_seconds == (300, 900, 3600, 14400)
    assert settings.bar_interval == "1m"
    assert settings.ideal_fetch_delay_seconds == 120
    assert settings.ideal_thesis_delay_seconds == 60


def test_from_env_parses_delay_buckets_and_flags():
    env = {
        "BACKTESTER_DELAY_BUCKETS_SECONDS": "60, 120 ,600",
        "BACKTESTER_PERSIST_EQUITY_POINTS": "false",
        "BACKTESTER_MAX_POSITION_USD": "2500",
        "THESIS_BUILDER_DB_SCHEMA": "tb_custom",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        settings = BacktesterSettings.from_env()
    assert settings.delay_buckets_seconds == (60, 120, 600)
    assert settings.persist_equity_points is False
    assert settings.max_position_usd == 2500.0
    assert settings.thesis_builder_db_schema == "tb_custom"


def test_postgres_dsn_uses_env():
    env = {"POSTGRES_HOST": "db.example", "POSTGRES_DATABASE": "trader_x"}
    with mock.patch.dict(os.environ, env, clear=True):
        dsn = BacktesterSettings.from_env().postgres_dsn
    assert "host=db.example" in dsn
    assert "dbname=trader_x" in dsn
