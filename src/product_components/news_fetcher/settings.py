from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NewsFetcherSettings:
    """Environment-backed NewsFetcher runtime settings."""

    newsfetcher_db_schema: str
    shared_db_schema: str
    watchlist_table: str

    news_poll_interval: int
    rss_poll_interval: int
    marketaux_poll_interval: int
    provider_timeout_seconds: int
    provider_max_retries: int
    provider_backoff_base_seconds: int

    queue_url: str
    news_raw_queue: str

    dedupe_lookback_hours: int
    dedupe_similarity_threshold: float
    dedupe_algorithm: str

    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]

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
    def from_env(cls) -> "NewsFetcherSettings":
        return cls(
            newsfetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
            shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
            watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
            news_poll_interval=_int_env("NEWS_POLL_INTERVAL", 120),
            rss_poll_interval=_int_env("RSS_POLL_INTERVAL", 300),
            marketaux_poll_interval=_int_env("MARKETAUX_POLL_INTERVAL", 300),
            provider_timeout_seconds=_int_env("PROVIDER_TIMEOUT_SECONDS", 10),
            provider_max_retries=_int_env("PROVIDER_MAX_RETRIES", 3),
            provider_backoff_base_seconds=_int_env("PROVIDER_BACKOFF_BASE_SECONDS", 1),
            queue_url=os.getenv("QUEUE_URL", "redis://127.0.0.1:6379/0"),
            news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
            dedupe_lookback_hours=_int_env("DEDUPE_LOOKBACK_HOURS", 24),
            dedupe_similarity_threshold=_float_env("DEDUPE_SIMILARITY_THRESHOLD", 0.9),
            dedupe_algorithm=os.getenv("DEDUPE_ALGORITHM", "rapidfuzz_ratio"),
            include_keywords=_csv_env("NEWS_INCLUDE_KEYWORDS"),
            exclude_keywords=_csv_env("NEWS_EXCLUDE_KEYWORDS"),
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


def _csv_env(key: str) -> tuple[str, ...]:
    raw = os.getenv(key, "")
    values = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    return tuple(values)
