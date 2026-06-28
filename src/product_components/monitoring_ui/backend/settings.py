from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class MonitoringUiSettings:
    """Environment-backed Monitoring UI runtime settings."""

    ui_port: int
    ui_api_base_url: str
    ui_refresh_interval_seconds: int
    ui_provider_refresh_interval_seconds: int
    ui_alerts_refresh_interval_seconds: int
    ui_query_timeout_seconds: int
    ui_stale_data_ttl_seconds: int
    ui_default_time_window: str
    ui_export_max_rows: int
    newsfetcher_db_schema: str
    filter_quality_db_schema: str
    thesis_builder_db_schema: str
    backtester_db_schema: str
    ui_backtest_refresh_interval_seconds: int
    shared_db_schema: str
    watchlist_table: str
    thesis_builder_evidence_collection_max_minutes: int
    filter_quality_run_timeout_seconds: int
    queue_url: str
    news_raw_queue: str
    failed_messages_dlq: str
    reprocess_command_queue: str
    massive_api_key: str
    massive_api_base_url: str
    alpha_vantage_api_key: str
    openfigi_api_key: str
    instrument_lookup_cache_ttl_seconds: int
    instrument_alias_cache_ttl_seconds: int
    instrument_lookup_provider_debounce_ms: int

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
    def from_env(cls) -> "MonitoringUiSettings":
        return cls(
            ui_port=_int_env("UI_PORT", 8080),
            ui_api_base_url=os.getenv("UI_API_BASE_URL", "http://localhost:8080/api"),
            ui_refresh_interval_seconds=_int_env("UI_REFRESH_INTERVAL_SECONDS", 15),
            ui_provider_refresh_interval_seconds=_int_env("UI_PROVIDER_REFRESH_INTERVAL_SECONDS", 10),
            ui_alerts_refresh_interval_seconds=_int_env("UI_ALERTS_REFRESH_INTERVAL_SECONDS", 20),
            ui_query_timeout_seconds=_int_env("UI_QUERY_TIMEOUT_SECONDS", 5),
            ui_stale_data_ttl_seconds=_int_env("UI_STALE_DATA_TTL_SECONDS", 120),
            ui_default_time_window=os.getenv("UI_DEFAULT_TIME_WINDOW", "1d"),
            ui_export_max_rows=_int_env("UI_EXPORT_MAX_ROWS", 500),
            newsfetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
            filter_quality_db_schema=os.getenv("FILTER_QUALITY_DB_SCHEMA", "filter_quality_evaluator"),
            thesis_builder_db_schema=os.getenv("THESIS_BUILDER_DB_SCHEMA", "thesis_builder"),
            backtester_db_schema=os.getenv("BACKTESTER_DB_SCHEMA", "backtester"),
            ui_backtest_refresh_interval_seconds=_int_env("UI_BACKTEST_REFRESH_INTERVAL_SECONDS", 15),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
            thesis_builder_evidence_collection_max_minutes=_int_env(
                "THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES",
                120,
            ),
            filter_quality_run_timeout_seconds=_int_env("FILTER_QUALITY_RUN_TIMEOUT_SECONDS", 1800),
            queue_url=_queue_url_from_env(),
            news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
            failed_messages_dlq=os.getenv("FAILED_MESSAGES_DLQ", "failed_messages_dlq"),
            reprocess_command_queue=os.getenv("REPROCESS_COMMAND_QUEUE", "reprocess_command_queue"),
            massive_api_key=os.getenv("MASSIVE_API_KEY", ""),
            massive_api_base_url=os.getenv("MASSIVE_API_BASE_URL", "https://api.polygon.io"),
            alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY", ""),
            openfigi_api_key=os.getenv("OPENFIGI_API_KEY", ""),
            instrument_lookup_cache_ttl_seconds=_int_env("INSTRUMENT_LOOKUP_CACHE_TTL_SECONDS", 604800),
            instrument_alias_cache_ttl_seconds=_int_env("INSTRUMENT_ALIAS_CACHE_TTL_SECONDS", 86400),
            instrument_lookup_provider_debounce_ms=_int_env("INSTRUMENT_LOOKUP_PROVIDER_DEBOUNCE_MS", 300),
        )


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


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
