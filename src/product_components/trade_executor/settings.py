from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# Canonical IBKR ports. The port — not the mode flag — determines which account
# receives orders, so the mode and port must agree in both directions.
PAPER_PORTS = frozenset({7497, 4002})  # 7497=TWS paper, 4002=Gateway paper
LIVE_PORTS = frozenset({7496, 4001})  # 7496=TWS live, 4001=Gateway live


class ModePortMismatchError(ValueError):
    """Raised when TRADE_EXECUTOR_TRADING_MODE and IBKR_PORT disagree."""


@dataclass(frozen=True)
class TradeExecutorSettings:
    """Environment-backed TradeExecutor runtime settings."""

    trade_executor_db_schema: str
    shared_db_schema: str
    market_data_db_schema: str
    watchlist_table: str

    trading_mode: str

    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int

    queue_url: str
    signal_queue: str
    failed_messages_dlq: str
    consumer_group: str
    consumer_name: str
    batch_size: int
    block_ms: int
    claim_min_idle_seconds: int
    max_delivery_attempts: int

    min_confidence: float
    quote_max_age_seconds: int

    atr_stop_mult: float
    take_profit_r: float

    entry_limit_slippage_bps: float
    order_fill_timeout_seconds: int
    outside_rth: bool

    time_horizon_days_map: dict[str, int]
    trading_day_timezone: str

    max_position_size: float
    max_positions: int
    max_portfolio_exposure: float
    max_sector_exposure: float
    daily_loss_limit: float
    max_daily_trades: int

    poll_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 60
    log_level: str = "INFO"
    log_file: str = "logs/trade-executor.log"

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

    def validate_mode_port_agreement(self) -> None:
        """Fail closed unless the trading mode and IBKR port agree.

        A ``paper`` mode pointed at a live-account port would silently trade a
        live account; a ``live`` mode pointed at a paper port is a
        misconfiguration. An unknown port (in neither canonical set) is refused
        too, so a typo'd port can never route orders to an unintended account.
        """
        if self.trading_mode not in {"paper", "live"}:
            raise ModePortMismatchError(
                f"TRADE_EXECUTOR_TRADING_MODE must be 'paper' or 'live', got {self.trading_mode!r}"
            )
        if self.trading_mode == "paper" and self.ibkr_port in LIVE_PORTS:
            raise ModePortMismatchError(
                f"paper mode pointed at live-account port {self.ibkr_port}; refusing to start"
            )
        if self.trading_mode == "live" and self.ibkr_port in PAPER_PORTS:
            raise ModePortMismatchError(
                f"live mode pointed at paper port {self.ibkr_port}; refusing to start"
            )
        if self.ibkr_port not in PAPER_PORTS and self.ibkr_port not in LIVE_PORTS:
            raise ModePortMismatchError(
                f"IBKR_PORT {self.ibkr_port} is not a recognized paper "
                f"({sorted(PAPER_PORTS)}) or live ({sorted(LIVE_PORTS)}) port; refusing to start"
            )

    @classmethod
    def from_env(cls) -> "TradeExecutorSettings":
        return cls(
            trade_executor_db_schema=os.getenv("TRADE_EXECUTOR_DB_SCHEMA", "trade_executor"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            market_data_db_schema=os.getenv("MARKET_DATA_DB_SCHEMA", "market_data"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
            trading_mode=os.getenv("TRADE_EXECUTOR_TRADING_MODE", "paper").strip().lower(),
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=_int_env("IBKR_PORT", 7497),
            ibkr_client_id=_int_env("IBKR_TRADE_EXECUTOR_CLIENT_ID", 5),
            queue_url=_queue_url_from_env(),
            signal_queue=os.getenv("SIGNAL_QUEUE", "signal_queue"),
            failed_messages_dlq=os.getenv("FAILED_MESSAGES_DLQ", "failed_messages_dlq"),
            consumer_group=os.getenv("TRADE_EXECUTOR_CONSUMER_GROUP", "trade_executor_group"),
            consumer_name=os.getenv("TRADE_EXECUTOR_CONSUMER_NAME", _default_consumer_name()),
            batch_size=_int_env("TRADE_EXECUTOR_BATCH_SIZE", 16),
            block_ms=_int_env("TRADE_EXECUTOR_BLOCK_MS", 5000),
            claim_min_idle_seconds=_int_env("TRADE_EXECUTOR_CLAIM_MIN_IDLE_SECONDS", 60),
            max_delivery_attempts=_int_env("TRADE_EXECUTOR_MAX_DELIVERY_ATTEMPTS", 5),
            min_confidence=_float_env("TRADE_EXECUTOR_MIN_CONFIDENCE", 0.6),
            quote_max_age_seconds=_int_env("TRADE_EXECUTOR_QUOTE_MAX_AGE_SECONDS", 30),
            atr_stop_mult=_float_env("ATR_STOP_MULT", 1.5),
            take_profit_r=_float_env("TAKE_PROFIT_R", 2.0),
            entry_limit_slippage_bps=_float_env("ENTRY_LIMIT_SLIPPAGE_BPS", 5.0),
            order_fill_timeout_seconds=_int_env("ORDER_FILL_TIMEOUT_SECONDS", 30),
            outside_rth=_bool_env("OUTSIDE_RTH", False),
            time_horizon_days_map=_horizon_map_env("TIME_HORIZON_DAYS_MAP", {"swing_1d_5d": 5}),
            trading_day_timezone=os.getenv("TRADING_DAY_TIMEZONE", "America/New_York"),
            max_position_size=_float_env("MAX_POSITION_SIZE", 1000.0),
            max_positions=_int_env("MAX_POSITIONS", 5),
            max_portfolio_exposure=_float_env("MAX_PORTFOLIO_EXPOSURE", 5000.0),
            max_sector_exposure=_float_env("MAX_SECTOR_EXPOSURE", 2500.0),
            daily_loss_limit=_float_env("DAILY_LOSS_LIMIT", 200.0),
            max_daily_trades=_int_env("MAX_DAILY_TRADES", 10),
            poll_interval_seconds=_int_env("TRADE_EXECUTOR_POLL_INTERVAL_SECONDS", 5),
            heartbeat_interval_seconds=_int_env("TRADE_EXECUTOR_HEARTBEAT_INTERVAL_SECONDS", 60),
            log_level=os.getenv("TRADE_EXECUTOR_LOG_LEVEL") or os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("TRADE_EXECUTOR_LOG_FILE", "logs/trade-executor.log"),
        )

    def log_file_path(self, repo_root: Path) -> Path | None:
        return _optional_path(self.log_file, repo_root)


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


def _horizon_map_env(key: str, default: dict[str, int]) -> dict[str, int]:
    """Parse ``horizon=days`` pairs (comma-separated) into a mapping.

    Example: ``"swing_1d_5d=5,swing_5d_20d=20"`` -> ``{"swing_1d_5d": 5, "swing_5d_20d": 20}``.
    Malformed pairs are skipped; an empty/absent value yields the default.
    """
    value = os.getenv(key)
    if value is None or not value.strip():
        return dict(default)
    mapping: dict[str, int] = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        horizon, days = chunk.split("=", 1)
        horizon = horizon.strip()
        try:
            mapping[horizon] = int(days.strip())
        except ValueError:
            continue
    return mapping or dict(default)


def _optional_path(value: str, repo_root: Path) -> Path | None:
    if not value.strip():
        return None
    path = Path(value.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path


def _default_consumer_name() -> str:
    # Stable across restarts (no PID) so a restart reclaims its own pending
    # entries via the id="0" read rather than leaking a new consumer row.
    hostname = os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "local"
    return f"trade_executor_{hostname}"


def _queue_url_from_env() -> str:
    queue_url = os.getenv("QUEUE_URL", "redis://127.0.0.1:6379/0").strip()
    redis_password = (os.getenv("REDIS_PASSWORD") or "").strip()
    if not redis_password:
        return queue_url

    parts = urlsplit(queue_url)
    if parts.scheme not in {"redis", "rediss"}:
        return queue_url
    if "@" in parts.netloc:
        return queue_url

    host_port = parts.netloc
    return urlunsplit(
        (
            parts.scheme,
            f":{quote(redis_password, safe='')}@{host_port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )
