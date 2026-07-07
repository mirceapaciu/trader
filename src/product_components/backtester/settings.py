from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BacktesterSettings:
    db_schema: str
    default_trigger_mode: str
    default_mode: str
    default_card_population: str
    require_time_window: bool
    default_lookback_days: int
    run_timeout_seconds: int
    default_timing_scenario: str
    ideal_fetch_delay_seconds: int
    ideal_thesis_delay_seconds: int
    delay_buckets_seconds: tuple[int, ...]
    initial_capital_usd: float
    risk_free_rate_annual: float
    sharpe_periodicity: str
    execution_mode: str
    bar_interval: str
    entry_slippage_bps: float
    exit_slippage_bps: float
    commission_model: str
    commission_per_share_usd: float
    commission_min_usd: float
    limit_order_validity_bars: int
    intrabar_stop_before_target: bool
    atr_stop_mult: float
    take_profit_r: float
    min_confidence: float
    entry_limit_slippage_bps: float
    excursion_horizon_minutes: tuple[int, ...]
    time_horizon_days_map: dict[str, int]
    max_position_usd: float
    max_positions: int
    max_portfolio_exposure_usd: float
    max_sector_exposure_usd: float
    max_positions_per_sector: int
    max_daily_trades: int
    daily_loss_limit_usd: float
    ticker_cooldown_minutes: int
    regeneration_enabled: bool
    llm_model: str
    llm_max_tokens_per_run: int
    llm_concurrency: int
    persist_equity_points: bool
    persist_card_snapshots: bool
    log_level: str
    market_data_db_schema: str
    thesis_builder_db_schema: str
    shared_db_schema: str

    @property
    def postgres_dsn(self) -> str:
        host = os.getenv("POSTGRES_HOST", "127.0.0.1")
        port = os.getenv("POSTGRES_PORT", "5432")
        database = os.getenv("POSTGRES_DATABASE", "trader")
        user = os.getenv("POSTGRES_USER", "trader")
        password = os.getenv("POSTGRES_PASSWORD", "")
        sslmode = os.getenv("POSTGRES_SSLMODE", "disable")
        return (
            f"host={host} port={port} dbname={database} user={user} "
            f"password={password} sslmode={sslmode}"
        )

    @classmethod
    def from_env(cls) -> "BacktesterSettings":
        return cls(
            db_schema=os.getenv("BACKTESTER_DB_SCHEMA", "backtester"),
            default_trigger_mode=os.getenv("BACKTESTER_DEFAULT_TRIGGER_MODE", "manual_cli"),
            default_mode=os.getenv("BACKTESTER_DEFAULT_MODE", "replay"),
            default_card_population=os.getenv("BACKTESTER_DEFAULT_CARD_POPULATION", "all"),
            require_time_window=_bool_env("BACKTESTER_REQUIRE_TIME_WINDOW", True),
            default_lookback_days=_int_env("BACKTESTER_DEFAULT_LOOKBACK_DAYS", 90),
            run_timeout_seconds=_int_env("BACKTESTER_RUN_TIMEOUT_SECONDS", 3600),
            default_timing_scenario=os.getenv("BACKTESTER_DEFAULT_TIMING_SCENARIO", "ideal"),
            ideal_fetch_delay_seconds=_int_env("BACKTESTER_IDEAL_FETCH_DELAY_SECONDS", 120),
            ideal_thesis_delay_seconds=_int_env("BACKTESTER_IDEAL_THESIS_DELAY_SECONDS", 60),
            delay_buckets_seconds=_int_tuple_env(
                "BACKTESTER_DELAY_BUCKETS_SECONDS", (300, 900, 3600, 14400)
            ),
            initial_capital_usd=_float_env("BACKTESTER_INITIAL_CAPITAL_USD", 10000.0),
            risk_free_rate_annual=_float_env("BACKTESTER_RISK_FREE_RATE_ANNUAL", 0.02),
            sharpe_periodicity=os.getenv("BACKTESTER_SHARPE_PERIODICITY", "daily"),
            execution_mode=os.getenv("BACKTESTER_EXECUTION_MODE", "live_parity"),
            bar_interval=os.getenv("BACKTESTER_BAR_INTERVAL", "1m"),
            entry_slippage_bps=_float_env("BACKTESTER_ENTRY_SLIPPAGE_BPS", 5.0),
            exit_slippage_bps=_float_env("BACKTESTER_EXIT_SLIPPAGE_BPS", 5.0),
            commission_model=os.getenv("BACKTESTER_COMMISSION_MODEL", "per_share"),
            commission_per_share_usd=_float_env("BACKTESTER_COMMISSION_PER_SHARE_USD", 0.005),
            commission_min_usd=_float_env("BACKTESTER_COMMISSION_MIN_USD", 1.0),
            limit_order_validity_bars=_int_env("BACKTESTER_LIMIT_ORDER_VALIDITY_BARS", 3),
            intrabar_stop_before_target=_bool_env("BACKTESTER_INTRABAR_STOP_BEFORE_TARGET", True),
            atr_stop_mult=_float_env("ATR_STOP_MULT", 1.5),
            take_profit_r=_float_env("TAKE_PROFIT_R", 2.0),
            min_confidence=_float_env("TRADE_EXECUTOR_MIN_CONFIDENCE", 0.6),
            entry_limit_slippage_bps=_float_env("ENTRY_LIMIT_SLIPPAGE_BPS", 5.0),
            excursion_horizon_minutes=_int_tuple_env(
                "BACKTESTER_EXCURSION_HORIZON_MINUTES",
                (30, 60, 120, 240, 390, 1170, 1950),
            ),
            time_horizon_days_map=_horizon_map_env("TIME_HORIZON_DAYS_MAP", {"swing_1d_5d": 5}),
            max_position_usd=_float_env("MAX_POSITION_SIZE", _float_env("BACKTESTER_MAX_POSITION_USD", 1000.0)),
            max_positions=_int_env("MAX_POSITIONS", 5),
            max_portfolio_exposure_usd=_float_env(
                "MAX_PORTFOLIO_EXPOSURE",
                _float_env("BACKTESTER_MAX_PORTFOLIO_EXPOSURE_USD", 5000.0),
            ),
            max_sector_exposure_usd=_float_env("MAX_SECTOR_EXPOSURE", 2500.0),
            max_positions_per_sector=_int_env("BACKTESTER_MAX_POSITIONS_PER_SECTOR", 3),
            max_daily_trades=_int_env("MAX_DAILY_TRADES", _int_env("BACKTESTER_MAX_DAILY_TRADES", 10)),
            daily_loss_limit_usd=_float_env(
                "DAILY_LOSS_LIMIT",
                _float_env("BACKTESTER_DAILY_LOSS_LIMIT_USD", 200.0),
            ),
            ticker_cooldown_minutes=_int_env("BACKTESTER_TICKER_COOLDOWN_MINUTES", 30),
            regeneration_enabled=_bool_env("BACKTESTER_REGENERATION_ENABLED", False),
            llm_model=os.getenv("BACKTESTER_LLM_MODEL", "gpt-4o-mini"),
            llm_max_tokens_per_run=_int_env("BACKTESTER_LLM_MAX_TOKENS_PER_RUN", 500000),
            llm_concurrency=max(1, _int_env("BACKTESTER_LLM_CONCURRENCY", 4)),
            persist_equity_points=_bool_env("BACKTESTER_PERSIST_EQUITY_POINTS", True),
            persist_card_snapshots=_bool_env("BACKTESTER_PERSIST_CARD_SNAPSHOTS", True),
            log_level=os.getenv("BACKTESTER_LOG_LEVEL", "INFO"),
            market_data_db_schema=os.getenv("MARKET_DATA_DB_SCHEMA", "market_data"),
            thesis_builder_db_schema=os.getenv("THESIS_BUILDER_DB_SCHEMA", "thesis_builder"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
        )


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


def _float_env(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return float(value)


def _bool_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _int_tuple_env(key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return tuple(int(part.strip()) for part in value.split(",") if part.strip())


def _horizon_map_env(key: str, default: dict[str, int]) -> dict[str, int]:
    value = os.getenv(key)
    if value is None or not value.strip():
        return dict(default)
    mapping: dict[str, int] = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        horizon, days = chunk.split("=", 1)
        try:
            mapping[horizon.strip()] = int(days.strip())
        except ValueError:
            continue
    return mapping or dict(default)
