from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
import redis

from tests.integration._db_test_helper import (
    bootstrap_newsfetcher_schema,
    db_config,
    ensure_postgres_access,
    ensure_safe_test_database,
    ensure_test_database_exists,
)
from src.core_components.event_ingestion_engine.errors import (
    NonTransientPublishError,
    TransientPublishError,
)
from src.product_components.news_fetcher.providers import (
    NewsProvider,
    ProviderArticle,
    ProviderBatch,
)
from src.product_components.news_fetcher.service import NewsFetcherService
from src.product_components.news_fetcher.settings import NewsFetcherSettings
from src.product_components.news_fetcher.storage_adapter import PostgresNewsStorageAdapter


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _prepare_test_database() -> None:
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    bootstrap_newsfetcher_schema(config)


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=(os.getenv("REDIS_PASSWORD") or None),
        socket_connect_timeout=5,
        socket_timeout=5,
        decode_responses=True,
    )


def _wait_for_redis(client: redis.Redis, timeout_seconds: float = 15.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if client.ping() is True:
                return
        except redis.exceptions.RedisError:
            pass
        time.sleep(0.5)
    pytest.skip("Redis is not reachable for integration test")


def _settings() -> NewsFetcherSettings:
    return NewsFetcherSettings(
        newsfetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
        shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
        watchlist_table=os.getenv("WATCHLIST_TABLE", "t_watchlist_tickers"),
        news_poll_interval=int(os.getenv("NEWS_POLL_INTERVAL", "120")),
        rss_poll_interval=int(os.getenv("RSS_POLL_INTERVAL", "300")),
        marketaux_poll_interval=int(os.getenv("MARKETAUX_POLL_INTERVAL", "300")),
        provider_timeout_seconds=int(os.getenv("PROVIDER_TIMEOUT_SECONDS", "10")),
        provider_max_retries=int(os.getenv("PROVIDER_MAX_RETRIES", "3")),
        provider_backoff_base_seconds=int(os.getenv("PROVIDER_BACKOFF_BASE_SECONDS", "1")),
        queue_url=os.getenv("QUEUE_URL", "redis://127.0.0.1:6379/0"),
        news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
        dedupe_lookback_hours=int(os.getenv("DEDUPE_LOOKBACK_HOURS", "24")),
        dedupe_similarity_threshold=float(os.getenv("DEDUPE_SIMILARITY_THRESHOLD", "0.9")),
        dedupe_algorithm=os.getenv("DEDUPE_ALGORITHM", "rapidfuzz_ratio"),
        include_keywords=tuple(),
        exclude_keywords=tuple(),
    )


class _StaticProvider(NewsProvider):
    def __init__(self, batch: ProviderBatch) -> None:
        self.batch = batch

    def fetch(self, *, source_key: str, cursor: Any | None, timeout_seconds: int) -> ProviderBatch:
        return self.batch


class _RedisPublisher:
    def __init__(self, *, client: redis.Redis, stream_name: str) -> None:
        self._client = client
        self._stream_name = stream_name

    def publish(self, envelope: dict[str, Any]) -> None:
        payload = {
            "event_id": str(envelope.get("event_id", "")),
            "event_type": str(envelope.get("event_type", "")),
            "dedupe_key": str(envelope.get("dedupe_key", "")),
        }
        self._client.xadd(self._stream_name, payload)


class _AlwaysFailPublisher:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def publish(self, envelope: dict[str, Any]) -> None:
        raise self._exc


def _seed_watchlist_ticker(settings: NewsFetcherSettings, ticker: str = "AAPL") -> None:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {settings.shared_db_schema}.{settings.watchlist_table}
                    (ticker, exchange_code, is_active, source, created_at, updated_at)
                VALUES (%s, %s, TRUE, %s, NOW(), NOW())
                ON CONFLICT (ticker, exchange_code)
                DO UPDATE SET is_active = TRUE, updated_at = NOW()
                """,
                (ticker, "NASDAQ", "integration_test"),
            )
        conn.commit()


def _cleanup_news_fetcher_rows(settings: NewsFetcherSettings) -> None:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {settings.newsfetcher_db_schema}.t_publication_obligations")
            cur.execute(f"DELETE FROM {settings.newsfetcher_db_schema}.t_news_articles")
            cur.execute(f"DELETE FROM {settings.newsfetcher_db_schema}.t_source_checkpoints")
        conn.commit()


def _count_rows(settings: NewsFetcherSettings, table_name: str) -> int:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {settings.newsfetcher_db_schema}.{table_name}")
            return int(cur.fetchone()[0])


def _obligation_statuses(settings: NewsFetcherSettings) -> list[str]:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT status FROM {settings.newsfetcher_db_schema}.t_publication_obligations ORDER BY obligation_id"
            )
            rows = cur.fetchall()
    return [str(row[0]) for row in rows]


def _latest_checkpoint_version(settings: NewsFetcherSettings, source_key: str) -> int | None:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT version FROM {settings.newsfetcher_db_schema}.t_source_checkpoints WHERE source_key = %s",
                (source_key,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else None


def _batch_with_one_article(provider_event_id: str) -> ProviderBatch:
    now = datetime.now(UTC).replace(microsecond=0)
    return ProviderBatch(
        events=[
            ProviderArticle(
                source="finnhub",
                provider_event_id=provider_event_id,
                headline="Apple raises guidance",
                summary="Quarterly outlook improved",
                url="https://example.com/news/aapl-guidance",
                tickers=["AAPL"],
                sentiment_source=0.7,
                published_at=now,
                fetched_at=now,
            )
        ],
        next_cursor={"min_id": 9999},
        cursor_updated_at=now,
    )


def test_news_fetcher_e2e_persist_then_publish_and_checkpoint() -> None:
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup_news_fetcher_rows(settings)
    _seed_watchlist_ticker(settings)

    stream = settings.news_raw_queue
    redis_client.delete(stream)

    service = NewsFetcherService(
        settings=settings,
        providers={"finnhub": _StaticProvider(_batch_with_one_article("evt-e2e-1"))},
        storage=PostgresNewsStorageAdapter(
            dsn=settings.postgres_dsn,
            news_schema=settings.newsfetcher_db_schema,
            shared_schema=settings.shared_db_schema,
            watchlist_table=settings.watchlist_table,
        ),
        publisher=_RedisPublisher(client=redis_client, stream_name=stream),
    )

    result = service.run_once()["finnhub"]

    assert result.accepted == 1
    assert _count_rows(settings, "t_news_articles") == 1
    assert _count_rows(settings, "t_publication_obligations") == 1
    assert _obligation_statuses(settings) == ["published"]
    assert _latest_checkpoint_version(settings, "finnhub") == 1
    assert redis_client.xlen(stream) >= 1


def test_news_fetcher_repeated_cycle_is_idempotent_on_articles() -> None:
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup_news_fetcher_rows(settings)
    _seed_watchlist_ticker(settings)

    service = NewsFetcherService(
        settings=settings,
        providers={"finnhub": _StaticProvider(_batch_with_one_article("evt-repeat-1"))},
        storage=PostgresNewsStorageAdapter(
            dsn=settings.postgres_dsn,
            news_schema=settings.newsfetcher_db_schema,
            shared_schema=settings.shared_db_schema,
            watchlist_table=settings.watchlist_table,
        ),
        publisher=_RedisPublisher(client=redis_client, stream_name=settings.news_raw_queue),
    )

    first = service.run_once()["finnhub"]
    second = service.run_once()["finnhub"]

    assert first.accepted == 1
    assert second.accepted in {0, 1}
    assert _count_rows(settings, "t_news_articles") == 1


def test_news_fetcher_non_transient_publish_moves_to_dead_lettered() -> None:
    settings = _settings()
    _cleanup_news_fetcher_rows(settings)
    _seed_watchlist_ticker(settings)

    service = NewsFetcherService(
        settings=settings,
        providers={"finnhub": _StaticProvider(_batch_with_one_article("evt-dlq-1"))},
        storage=PostgresNewsStorageAdapter(
            dsn=settings.postgres_dsn,
            news_schema=settings.newsfetcher_db_schema,
            shared_schema=settings.shared_db_schema,
            watchlist_table=settings.watchlist_table,
        ),
        publisher=_AlwaysFailPublisher(NonTransientPublishError("invalid_envelope")),
    )

    result = service.run_once()["finnhub"]

    assert result.accepted == 1
    assert _obligation_statuses(settings) == ["dead_lettered"]
    assert _latest_checkpoint_version(settings, "finnhub") == 1


def test_news_fetcher_transient_publish_failure_blocks_checkpoint() -> None:
    settings = _settings()
    _cleanup_news_fetcher_rows(settings)
    _seed_watchlist_ticker(settings)

    service = NewsFetcherService(
        settings=settings,
        providers={"finnhub": _StaticProvider(_batch_with_one_article("evt-transient-1"))},
        storage=PostgresNewsStorageAdapter(
            dsn=settings.postgres_dsn,
            news_schema=settings.newsfetcher_db_schema,
            shared_schema=settings.shared_db_schema,
            watchlist_table=settings.watchlist_table,
        ),
        publisher=_AlwaysFailPublisher(TransientPublishError("temporary_broker_failure")),
    )

    result = service.run_once()["finnhub"]

    assert result.accepted == 1
    assert _obligation_statuses(settings) == ["pending"]
    assert _latest_checkpoint_version(settings, "finnhub") is None
