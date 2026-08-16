from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class MonitoringUiSettings:
    """Environment-backed Monitoring UI runtime settings."""

    ui_host: str
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
    backtester_db_schema: str
    ui_backtest_refresh_interval_seconds: int
    shared_db_schema: str
    watchlist_table: str
    ui_thesis_builder_stall_threshold_seconds: int
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
    taxonomy_command_queue: str = "taxonomy_command_queue"
    taxonomy_decisions_enabled: bool = False
    taxonomy_trusted_actor_header: str = ""
    admin_password: str = ""
    admin_session_ttl_seconds: int = 28800
    admin_login_window_seconds: int = 900
    admin_login_max_attempts: int = 5
    admin_allowed_origin: str = ""

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
        settings = cls(
            ui_host=os.getenv("UI_HOST", "127.0.0.1"),
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
            backtester_db_schema=os.getenv("BACKTESTER_DB_SCHEMA", "backtester"),
            ui_backtest_refresh_interval_seconds=_int_env("UI_BACKTEST_REFRESH_INTERVAL_SECONDS", 15),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
            ui_thesis_builder_stall_threshold_seconds=_int_env(
                "UI_THESIS_BUILDER_STALL_THRESHOLD_SECONDS",
                600,
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
            taxonomy_command_queue=os.getenv(
                "THESIS_BUILDER_TAXONOMY_COMMAND_QUEUE",
                "taxonomy_command_queue",
            ),
            taxonomy_decisions_enabled=_bool_env(
                "UI_TAXONOMY_DECISIONS_ENABLED", False
            ),
            taxonomy_trusted_actor_header=os.getenv(
                "UI_TAXONOMY_TRUSTED_ACTOR_HEADER", ""
            ).strip(),
            admin_password=os.getenv("UI_ADMIN_PASSWORD", "").strip(),
            admin_session_ttl_seconds=_int_env("UI_ADMIN_SESSION_TTL_SECONDS", 28800),
            admin_login_window_seconds=_int_env("UI_ADMIN_LOGIN_WINDOW_SECONDS", 900),
            admin_login_max_attempts=_int_env("UI_ADMIN_LOGIN_MAX_ATTEMPTS", 5),
            admin_allowed_origin=os.getenv("UI_ADMIN_ALLOWED_ORIGIN", "").strip().rstrip("/"),
        )
        settings.validate_admin_auth()
        return settings

    def validate_admin_auth(self) -> None:
        if not self.taxonomy_decisions_enabled:
            return
        if self.taxonomy_trusted_actor_header:
            raise ValueError("UI_TAXONOMY_TRUSTED_ACTOR_HEADER is incompatible with single-admin authentication")
        if not self.admin_password:
            raise ValueError("UI_ADMIN_PASSWORD must be configured when taxonomy decisions are enabled")
        if min(self.admin_session_ttl_seconds, self.admin_login_window_seconds, self.admin_login_max_attempts) <= 0:
            raise ValueError("administrator authentication limits must be positive")
        origin = self.admin_allowed_origin or self.ui_api_base_url.removesuffix("/api")
        local_host = self.ui_host in {"127.0.0.1", "localhost", "::1"}
        if not origin.startswith(("http://", "https://")):
            raise ValueError("UI_ADMIN_ALLOWED_ORIGIN must name the public HTTPS UI origin")
        if not local_host and not origin.startswith("https://"):
            raise ValueError("single-admin authentication requires HTTPS outside loopback development")


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


def _bool_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
