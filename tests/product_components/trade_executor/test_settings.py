from src.product_components.trade_executor.settings import TradeExecutorSettings


def _clear(monkeypatch) -> None:
    for key in (
        "TRADE_EXECUTOR_DB_SCHEMA",
        "TRADE_EXECUTOR_TRADING_MODE",
        "IBKR_PORT",
        "IBKR_TRADE_EXECUTOR_CLIENT_ID",
        "SIGNAL_QUEUE",
        "TRADE_EXECUTOR_CONSUMER_GROUP",
        "TRADE_EXECUTOR_BATCH_SIZE",
        "TRADE_EXECUTOR_MIN_CONFIDENCE",
        "TRADE_EXECUTOR_QUOTE_MAX_AGE_SECONDS",
        "ATR_STOP_MULT",
        "TAKE_PROFIT_R",
        "ENTRY_LIMIT_SLIPPAGE_BPS",
        "ORDER_FILL_TIMEOUT_SECONDS",
        "OUTSIDE_RTH",
        "TIME_HORIZON_DAYS_MAP",
        "TRADING_DAY_TIMEZONE",
        "MAX_POSITION_SIZE",
        "MAX_POSITIONS",
        "MAX_PORTFOLIO_EXPOSURE",
        "MAX_SECTOR_EXPOSURE",
        "DAILY_LOSS_LIMIT",
        "MAX_DAILY_TRADES",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch) -> None:
    _clear(monkeypatch)

    settings = TradeExecutorSettings.from_env()

    assert settings.trade_executor_db_schema == "trade_executor"
    assert settings.shared_db_schema == "shared"
    assert settings.market_data_db_schema == "market_data"
    assert settings.trading_mode == "paper"
    assert settings.ibkr_port == 7497
    assert settings.ibkr_client_id == 5
    assert settings.signal_queue == "signal_queue"
    assert settings.consumer_group == "trade_executor_group"
    assert settings.batch_size == 16
    assert settings.block_ms == 5000
    assert settings.claim_min_idle_seconds == 60
    assert settings.max_delivery_attempts == 5
    assert settings.min_confidence == 0.6
    assert settings.quote_max_age_seconds == 30
    assert settings.atr_stop_mult == 1.5
    assert settings.take_profit_r == 2.0
    assert settings.entry_limit_slippage_bps == 5.0
    assert settings.order_fill_timeout_seconds == 30
    assert settings.outside_rth is False
    assert settings.time_horizon_days_map == {"swing_1d_5d": 5}
    assert settings.trading_day_timezone == "America/New_York"
    assert settings.max_position_size == 1000.0
    assert settings.max_positions == 5
    assert settings.max_portfolio_exposure == 5000.0
    assert settings.max_sector_exposure == 2500.0
    assert settings.daily_loss_limit == 200.0
    assert settings.max_daily_trades == 10


def test_env_override(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("TRADE_EXECUTOR_DB_SCHEMA", "te_test")
    monkeypatch.setenv("TRADE_EXECUTOR_TRADING_MODE", "live")
    monkeypatch.setenv("IBKR_PORT", "4001")
    monkeypatch.setenv("SIGNAL_QUEUE", "signal_test")
    monkeypatch.setenv("TRADE_EXECUTOR_BATCH_SIZE", "3")
    monkeypatch.setenv("ATR_STOP_MULT", "2.0")
    monkeypatch.setenv("TAKE_PROFIT_R", "3.0")
    monkeypatch.setenv("OUTSIDE_RTH", "true")
    monkeypatch.setenv("MAX_POSITIONS", "9")

    settings = TradeExecutorSettings.from_env()

    assert settings.trade_executor_db_schema == "te_test"
    assert settings.trading_mode == "live"
    assert settings.ibkr_port == 4001
    assert settings.signal_queue == "signal_test"
    assert settings.batch_size == 3
    assert settings.atr_stop_mult == 2.0
    assert settings.take_profit_r == 3.0
    assert settings.outside_rth is True
    assert settings.max_positions == 9


def test_time_horizon_map_parsing(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("TIME_HORIZON_DAYS_MAP", "swing_1d_5d=5, swing_5d_20d=20 , bad_entry, x=notint")

    settings = TradeExecutorSettings.from_env()

    assert settings.time_horizon_days_map == {"swing_1d_5d": 5, "swing_5d_20d": 20}


def test_bool_env_variants(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OUTSIDE_RTH", "no")
    assert TradeExecutorSettings.from_env().outside_rth is False
    monkeypatch.setenv("OUTSIDE_RTH", "ON")
    assert TradeExecutorSettings.from_env().outside_rth is True


def test_postgres_dsn(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DATABASE", "trader_test")
    monkeypatch.setenv("POSTGRES_USER", "tu")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")

    dsn = TradeExecutorSettings.from_env().postgres_dsn

    assert "host=db.example" in dsn
    assert "port=5433" in dsn
    assert "dbname=trader_test" in dsn
    assert "user=tu" in dsn
