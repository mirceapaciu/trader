from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterQualityEvaluatorSettings:
    db_schema: str
    default_trigger_mode: str
    max_items_per_run: int
    batch_size: int
    default_lookback_hours: int
    require_time_window: bool
    allow_config_fingerprint_filter: bool
    accepted_audit_enabled: bool
    accepted_audit_sample_size: int
    classification_concurrency: int
    llm_model: str
    llm_max_tokens_per_run: int
    llm_max_tokens_per_item: int
    min_confidence_threshold: float
    include_item_rationale: bool
    include_improvement_suggestion: bool
    run_timeout_seconds: int
    log_level: str
    newsfetcher_db_schema: str
    shared_db_schema: str
    watchlist_table: str

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
    def from_env(cls) -> "FilterQualityEvaluatorSettings":
        return cls(
            db_schema=os.getenv("FILTER_QUALITY_DB_SCHEMA", "filter_quality_evaluator"),
            default_trigger_mode=os.getenv("FILTER_QUALITY_DEFAULT_TRIGGER_MODE", "manual_cli"),
            max_items_per_run=_int_env("FILTER_QUALITY_MAX_ITEMS_PER_RUN", 1000),
            batch_size=_int_env("FILTER_QUALITY_BATCH_SIZE", 50),
            default_lookback_hours=_int_env("FILTER_QUALITY_DEFAULT_LOOKBACK_HOURS", 24),
            require_time_window=_bool_env("FILTER_QUALITY_REQUIRE_TIME_WINDOW", True),
            allow_config_fingerprint_filter=_bool_env("FILTER_QUALITY_ALLOW_CONFIG_FINGERPRINT_FILTER", True),
            accepted_audit_enabled=_bool_env("FILTER_QUALITY_ACCEPTED_AUDIT_ENABLED", False),
            accepted_audit_sample_size=_int_env("FILTER_QUALITY_ACCEPTED_AUDIT_SAMPLE_SIZE", 200),
            classification_concurrency=max(1, _int_env("FILTER_QUALITY_CLASSIFICATION_CONCURRENCY", 4)),
            llm_model=os.getenv("FILTER_QUALITY_LLM_MODEL", "gpt-4o-mini"),
            llm_max_tokens_per_run=_int_env("FILTER_QUALITY_LLM_MAX_TOKENS_PER_RUN", 200000),
            llm_max_tokens_per_item=_int_env("FILTER_QUALITY_LLM_MAX_TOKENS_PER_ITEM", 1500),
            min_confidence_threshold=_float_env("FILTER_QUALITY_MIN_CONFIDENCE_THRESHOLD", 0.60),
            include_item_rationale=_bool_env("FILTER_QUALITY_INCLUDE_ITEM_RATIONALE", True),
            include_improvement_suggestion=_bool_env(
                "FILTER_QUALITY_INCLUDE_IMPROVEMENT_SUGGESTION",
                True,
            ),
            run_timeout_seconds=_int_env("FILTER_QUALITY_RUN_TIMEOUT_SECONDS", 1800),
            log_level=os.getenv("FILTER_QUALITY_LOG_LEVEL", "INFO"),
            newsfetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
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
