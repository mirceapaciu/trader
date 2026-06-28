from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MarketDataSettings:
    market_data_db_schema: str
    shared_db_schema: str
    watchlist_table: str
    primary_provider: str
    backfill_provider: str
    allow_delayed: bool
    quote_max_age_seconds: int
    context_max_age_seconds: int
    daily_bar_lookback_days: int
    market_hours_refresh_seconds: int
    off_hours_refresh_seconds: int
    ibkr_host: str
    ibkr_port: int
    ibkr_market_data_client_id: int
    alpha_vantage_api_key: str
    polygon_api_key: str
    polygon_api_base_url: str
    polygon_max_requests_per_minute: int
    historical_bars_provider: str

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
    def from_env(cls) -> "MarketDataSettings":
        return cls(
            market_data_db_schema=os.getenv("MARKET_DATA_DB_SCHEMA", "market_data"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
            primary_provider=os.getenv("MARKET_DATA_PRIMARY_PROVIDER", "ibkr"),
            backfill_provider=os.getenv("MARKET_DATA_BACKFILL_PROVIDER", "alpha_vantage"),
            allow_delayed=_bool_env("MARKET_DATA_ALLOW_DELAYED", True),
            quote_max_age_seconds=_int_env("MARKET_DATA_QUOTE_MAX_AGE_SECONDS", 1200),
            context_max_age_seconds=_int_env("MARKET_DATA_CONTEXT_MAX_AGE_SECONDS", 1800),
            daily_bar_lookback_days=_int_env("MARKET_DATA_DAILY_BAR_LOOKBACK_DAYS", 90),
            market_hours_refresh_seconds=_int_env("MARKET_DATA_MARKET_HOURS_REFRESH_SECONDS", 120),
            off_hours_refresh_seconds=_int_env("MARKET_DATA_OFF_HOURS_REFRESH_SECONDS", 1800),
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=_int_env("IBKR_PORT", 7497),
            ibkr_market_data_client_id=_int_env("IBKR_MARKET_DATA_CLIENT_ID", 2),
            alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY", ""),
            # Reuse the existing Massive/Polygon key for historical bar backfills.
            polygon_api_key=os.getenv("POLYGON_API_KEY") or os.getenv("MASSIVE_API_KEY", ""),
            polygon_api_base_url=os.getenv("POLYGON_API_BASE_URL", "https://api.polygon.io"),
            polygon_max_requests_per_minute=_int_env("POLYGON_MAX_REQUESTS_PER_MINUTE", 5),
            historical_bars_provider=os.getenv("MARKET_DATA_HISTORICAL_BARS_PROVIDER", "polygon"),
        )


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


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
