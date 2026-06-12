from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from src.core_components.event_ingestion_engine.models import FilterRun, FilterRunMode


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
    rss_enabled: bool
    rss_rate_limit_backoff_seconds: int
    instruments_config: str
    rss_sources_config: str
    legacy_rss_feed_urls: tuple[str, ...]

    queue_url: str
    news_raw_queue: str
    publish_retry_drain_batch_size: int

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
            rss_enabled=_bool_env("NEWS_SOURCE_RSS_ENABLED", True),
            rss_rate_limit_backoff_seconds=_int_env("RSS_RATE_LIMIT_BACKOFF_SECONDS", 900),
            instruments_config=os.getenv(
                "NEWS_INSTRUMENTS_CONFIG",
                "config/news-fetcher/instruments.json",
            ),
            rss_sources_config=os.getenv(
                "NEWS_RSS_SOURCES_CONFIG",
                "config/news-fetcher/rss-sources.json",
            ),
            legacy_rss_feed_urls=_csv_env_raw("RSS_FEED_URLS"),
            queue_url=_queue_url_from_env(),
            news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
            publish_retry_drain_batch_size=_int_env("NEWS_PUBLISH_RETRY_DRAIN_BATCH_SIZE", 500),
            dedupe_lookback_hours=_int_env("DEDUPE_LOOKBACK_HOURS", 24),
            dedupe_similarity_threshold=_float_env("DEDUPE_SIMILARITY_THRESHOLD", 0.9),
            dedupe_algorithm=os.getenv("DEDUPE_ALGORITHM", "rapidfuzz_ratio"),
            include_keywords=_csv_env("NEWS_INCLUDE_KEYWORDS"),
            exclude_keywords=_csv_env("NEWS_EXCLUDE_KEYWORDS"),
        )

    def instruments_config_path(self, repo_root: Path) -> Path | None:
        return _optional_path(self.instruments_config, repo_root)

    def rss_sources_config_path(self, repo_root: Path) -> Path | None:
        return _optional_path(self.rss_sources_config, repo_root)

    def filter_config_fingerprint(self, watchlist_tickers: set[str]) -> str:
        payload = {
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "watchlist_tickers": sorted(watchlist_tickers),
            "dedupe_algorithm": self.dedupe_algorithm,
            "dedupe_similarity_threshold": self.dedupe_similarity_threshold,
            "dedupe_lookback_hours": self.dedupe_lookback_hours,
        }
        normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def production_filter_run(self, watchlist_tickers: set[str]) -> FilterRun:
        fingerprint = self.filter_config_fingerprint(watchlist_tickers)
        return FilterRun(
            filter_run_id=f"prod_{fingerprint[:24]}",
            run_mode=FilterRunMode.PRODUCTION,
            filter_config_fingerprint=fingerprint,
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


def _csv_env_raw(key: str) -> tuple[str, ...]:
    raw = os.getenv(key, "")
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


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


def _optional_path(value: str, repo_root: Path) -> Path | None:
    if not value.strip():
        return None
    path = Path(value.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path


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
