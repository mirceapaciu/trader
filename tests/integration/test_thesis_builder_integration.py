from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
import redis

from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    PostgresSharedThesisCardReviewWriter,
    SharedWatchlistEntryInput,
)
from src.product_components.thesis_builder.llm_client import ThesisAnalyzer
from src.product_components.thesis_builder.models import (
    ContentType,
    LlmAnalysisResult,
    NewsArticle,
    ThesisStrategy,
    TradeDirection,
)
from src.product_components.thesis_builder.redis_io import RedisThesisBuilderIo
from src.product_components.thesis_builder.repository import PostgresThesisBuilderRepository
from src.product_components.thesis_builder.service import ThesisBuilderRunner
from src.product_components.thesis_builder.settings import ThesisBuilderSettings
from tests.integration._db_test_helper import (
    bootstrap_newsfetcher_schema,
    db_config,
    ensure_postgres_access,
    ensure_safe_test_database,
    ensure_test_database_exists,
)
from tests.integration._redis_test_helper import ensure_safe_test_redis, redis_config


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _prepare_test_database() -> None:
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    bootstrap_newsfetcher_schema(config)


@pytest.fixture(scope="module", autouse=True)
def _prepare_test_redis() -> None:
    ensure_safe_test_redis(redis_config())


def test_thesis_builder_persists_insufficient_evidence_without_signal() -> None:
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)
    _seed_watchlist(settings)
    _publish_news(redis_client, settings.news_raw_queue, ["article-1"])

    runner = _runner(settings)
    runner.bootstrap()
    runner.run_once()

    assert _count(settings, "t_news_analyses") == 1
    assert _count(settings, "t_evidence_windows") == 1
    assert _count(settings, "t_thesis_cards") == 0
    assert redis_client.xlen(settings.signal_queue) == 0


def test_thesis_builder_creates_preapproved_card_and_signal() -> None:
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)
    _seed_watchlist(settings)
    _publish_news(redis_client, settings.news_raw_queue, ["article-1", "article-2", "article-3"])

    runner = _runner(settings)
    runner.bootstrap()
    runner.run_once()

    assert _count(settings, "t_news_analyses") == 3
    assert _count(settings, "t_thesis_cards") == 1
    assert _count_shared_reviews(settings) == 1
    assert redis_client.xlen(settings.signal_queue) == 1


def test_short_alias_substring_does_not_tag_instrument() -> None:
    # Layer 1: the "mu" alias must not match inside "multi-trillion-dollar".
    # The article never names Micron, so no analysis should be produced for MU.
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)
    _seed_micron_watchlist(settings)
    _publish_untagged_listicle(redis_client, settings.news_raw_queue, "listicle-1")

    runner = _runner(settings)
    runner.bootstrap()
    runner.run_once()

    assert _count(settings, "t_news_analyses") == 0
    assert _count(settings, "t_evidence_windows") == 0
    assert redis_client.xlen(settings.signal_queue) == 0


def test_off_topic_analysis_is_rejected_by_relevance_gate() -> None:
    # Layer 2: an article that legitimately matches but that the LLM judges is not
    # about the instrument must be persisted as rejected, with no card or signal.
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)
    _seed_watchlist(settings)
    _publish_news(redis_client, settings.news_raw_queue, ["article-1"])

    runner = _runner(
        settings,
        llm_client=_FakeLlmClient(overrides={"instrument_is_subject": False}),
    )
    runner.bootstrap()
    runner.run_once()

    assert _count(settings, "t_news_analyses") == 1
    assert _rejection_reason_codes(settings) == ["instrument_not_subject"]
    assert _count(settings, "t_thesis_cards") == 0
    assert redis_client.xlen(settings.signal_queue) == 0


def test_opinion_is_retained_for_analyst_without_card() -> None:
    # A valuation opinion (e.g. "near 52-week low, undervalued") that is genuinely about
    # the instrument and reads bullishly must NOT produce a thesis, but must be retained
    # and queryable for the future stock-analyst component.
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)
    _seed_watchlist(settings)
    _publish_news(redis_client, settings.news_raw_queue, ["article-1", "article-2", "article-3"])

    runner = _runner(
        settings,
        llm_client=_FakeLlmClient(overrides={"content_type": ContentType.OPINION.value}),
    )
    runner.bootstrap()
    runner.run_once()

    assert _count(settings, "t_news_analyses") == 3
    assert _rejection_reason_codes(settings) == ["routed_to_analyst"] * 3
    assert _content_types(settings) == [ContentType.OPINION.value] * 3
    assert _count(settings, "t_evidence_windows") == 0
    assert _count(settings, "t_thesis_cards") == 0
    assert redis_client.xlen(settings.signal_queue) == 0


def test_rolling_window_ages_out_old_evidence_instead_of_swallowing_new() -> None:
    # Regression: an analysis arriving after the collection span used to be appended to
    # the window it had just expired and then discarded, so a cluster of articles that
    # straddled the anchor boundary could never form a card. With rolling semantics the
    # oldest evidence ages out and the window keeps collecting.
    settings = _settings()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)
    _cleanup(settings, redis_client)

    runner = _runner(settings)
    runner.bootstrap()
    repository = PostgresThesisBuilderRepository(
        dsn=settings.postgres_dsn,
        thesis_schema=settings.thesis_builder_db_schema,
    )
    base = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    def persist(index: int, offset_minutes: int):
        published_at = base + timedelta(minutes=offset_minutes)
        article_id = f"article-{index}"
        return repository.persist_analysis_and_update_evidence(
            article=_news_article(article_id, published_at),
            result=_buy_analysis(article_id),
            market_context_snapshot=None,
            required_evidence_count=3,
            min_confidence=settings.min_confidence,
            min_relevance=settings.min_relevance,
            risk_max_loss_usd=settings.risk_max_loss_usd,
            default_time_horizon=settings.default_time_horizon,
            evidence_collection_max_minutes=120,
            max_evidence_age_minutes=180,
            clock=lambda: published_at + timedelta(seconds=30),
        )

    # Articles at +0, +60 and +130 minutes: the third arrival is past the 120-minute
    # span of the first, so article-0 ages out and the window keeps collecting.
    for index, offset_minutes in enumerate([0, 60, 130]):
        assert persist(index, offset_minutes).signal is None
    assert _evidence_window_rows(settings) == [("collecting", ["article-1", "article-2"])]

    # The fourth article (+170) completes 3 pieces of evidence within 120 minutes.
    outcome = persist(3, 170)
    assert outcome.signal is not None
    assert outcome.signal.ticker == "AAPL"
    assert _count(settings, "t_thesis_cards") == 1
    assert _evidence_window_rows(settings) == [
        ("satisfied", ["article-1", "article-2", "article-3"])
    ]


def _news_article(article_id: str, published_at: datetime) -> NewsArticle:
    return NewsArticle(
        id=article_id,
        source="integration",
        headline=f"Apple raises guidance {article_id}",
        summary="Apple raised revenue guidance.",
        url=f"https://example.com/{article_id}",
        tickers=["AAPL"],
        published_at=published_at,
        fetched_at=published_at,
        sentiment_source=0.8,
    )


def _buy_analysis(article_id: str) -> LlmAnalysisResult:
    return LlmAnalysisResult(
        ticker="AAPL",
        exchange_code="XNAS",
        sentiment=0.8,
        relevance=0.9,
        urgency="today",
        suggested_action="buy",
        candidate_strategy=ThesisStrategy.EVENT_DRIVEN,
        direction=TradeDirection.BUY,
        confidence=0.75,
        reasoning=f"{article_id} supports a bullish event-driven thesis.",
        is_market_moving=True,
        instrument_is_subject=True,
        content_type=ContentType.NEWS_CATALYST,
        event_type="guidance",
        price_impact_magnitude="medium",
        evidence_bullet_candidates=[article_id],
        estimated_tokens=100,
        llm_model="test-model",
    )


def _evidence_window_rows(settings: ThesisBuilderSettings) -> list[tuple[str, list[str]]]:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT status, article_ids FROM {settings.thesis_builder_db_schema}.t_evidence_windows "
                f"ORDER BY id"
            )
            return [(row[0], list(row[1])) for row in cur.fetchall()]


def _runner(
    settings: ThesisBuilderSettings, *, llm_client: "_FakeLlmClient | None" = None
) -> ThesisBuilderRunner:
    return ThesisBuilderRunner(
        settings=settings,
        repository=PostgresThesisBuilderRepository(
            dsn=settings.postgres_dsn,
            thesis_schema=settings.thesis_builder_db_schema,
        ),
        instrument_registry=PostgresSharedInstrumentRegistry(
            dsn=settings.postgres_dsn,
            shared_schema=settings.shared_db_schema,
            watchlist_table="t_watchlist_tickers",
        ),
        review_writer=PostgresSharedThesisCardReviewWriter(
            dsn=settings.postgres_dsn,
            shared_schema=settings.shared_db_schema,
        ),
        redis_io=RedisThesisBuilderIo(
            queue_url=settings.queue_url,
            news_raw_queue=settings.news_raw_queue,
            signal_queue=settings.signal_queue,
            failed_messages_dlq=settings.failed_messages_dlq,
            consumer_group=settings.consumer_group,
            consumer_name=settings.consumer_name,
        ),
        analyzer=ThesisAnalyzer(
            client=llm_client or _FakeLlmClient(),
            model="test-model",
            max_tokens_per_run=100000,
            max_tokens_per_item=500,
        ),
    )


class _FakeLlmClient:
    """Deterministic stand-in for the OpenAI client.

    ``overrides`` lets a test force specific fields (e.g. ``instrument_is_subject``
    or ``relevance``) to exercise the relevance/grounding gate.
    """

    def __init__(self, overrides: dict | None = None) -> None:
        self._overrides = overrides or {}

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        payload = json.loads(prompt)
        result = {
            "ticker": payload["instrument"]["ticker"],
            "exchange_code": payload["instrument"]["exchange_code"],
            "sentiment": 0.8,
            "relevance": 0.9,
            "urgency": "today",
            "suggested_action": "buy",
            "candidate_strategy": ThesisStrategy.EVENT_DRIVEN.value,
            "direction": TradeDirection.BUY.value,
            "confidence": 0.75,
            "reasoning": f"{payload['article']['headline']} supports a bullish event-driven thesis.",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "content_type": ContentType.NEWS_CATALYST.value,
            "event_type": "guidance",
            "price_impact_magnitude": "medium",
            "evidence_bullet_candidates": [payload["article"]["headline"]],
            "estimated_tokens": 100,
        }
        result.update(self._overrides)
        return result


def _settings() -> ThesisBuilderSettings:
    return ThesisBuilderSettings(
        thesis_builder_db_schema=os.getenv("THESIS_BUILDER_DB_SCHEMA", "thesis_builder"),
        shared_db_schema=os.getenv("SHARED_DB_SCHEMA", "shared"),
        news_fetcher_db_schema=os.getenv("NEWSFETCHER_DB_SCHEMA", "news_fetcher"),
        queue_url=_queue_url(),
        news_raw_queue=os.getenv("NEWS_RAW_QUEUE", "news_raw_queue"),
        signal_queue=os.getenv("SIGNAL_QUEUE", "signal_queue"),
        failed_messages_dlq=os.getenv("FAILED_MESSAGES_DLQ", "failed_messages_dlq"),
        reprocess_command_queue=os.getenv("REPROCESS_COMMAND_QUEUE", "reprocess_command_queue"),
        reprocess_max_articles=200,
        consumer_group="thesis_builder_integration_group",
        consumer_name="thesis_builder_integration_consumer",
        poll_interval_seconds=1,
        heartbeat_interval_seconds=60,
        batch_size=10,
        block_ms=100,
        claim_min_idle_seconds=300,
        max_delivery_attempts=3,
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
        required_evidence_count=3,
        min_confidence=0.6,
        min_relevance=0.5,
        contrarian_min_confidence=0.72,
        trend_follow_min_confidence=0.68,
        risk_max_loss_usd=120.0,
        default_time_horizon="swing_1d_5d",
        llm_model="test-model",
        llm_daily_token_budget=100000,
        llm_max_output_tokens=500,
        llm_request_timeout_seconds=60.0,
        llm_max_retries=2,
        openai_api_key="test-key",
    )


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


def _queue_url() -> str:
    queue_url = os.getenv("QUEUE_URL", "redis://127.0.0.1:6379/0")
    password = (os.getenv("REDIS_PASSWORD") or "").strip()
    redis_db = os.getenv("REDIS_DB")
    if redis_db is not None:
        parts = urlsplit(queue_url)
        queue_url = urlunsplit((parts.scheme, parts.netloc, f"/{redis_db}", parts.query, parts.fragment))
    if not password:
        return queue_url
    parts = urlsplit(queue_url)
    if "@" in parts.netloc:
        return queue_url
    return urlunsplit(
        (
            parts.scheme,
            f":{quote(password, safe='')}@{parts.netloc}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _wait_for_redis(client: redis.Redis) -> None:
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Redis is not reachable for integration test: {exc}")


def _cleanup(settings: ThesisBuilderSettings, redis_client: redis.Redis) -> None:
    redis_client.delete(settings.news_raw_queue, settings.signal_queue, settings.failed_messages_dlq)
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {settings.shared_db_schema}.t_thesis_card_reviews")
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_thesis_cards")
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_evidence_windows")
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_news_analyses")
            cur.execute(f"DELETE FROM {settings.shared_db_schema}.t_watchlist_tickers")
        conn.commit()


def _seed_watchlist(settings: ThesisBuilderSettings) -> None:
    PostgresSharedInstrumentAdmin(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
    ).upsert_watchlist_entry(
        SharedWatchlistEntryInput(
            ticker="AAPL",
            exchange_code="XNAS",
            display_name="Apple Inc.",
            source="integration_test",
        )
    )


def _seed_micron_watchlist(settings: ThesisBuilderSettings) -> None:
    PostgresSharedInstrumentAdmin(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
    ).upsert_watchlist_entry(
        SharedWatchlistEntryInput(
            ticker="MU",
            exchange_code="XNAS",
            display_name="Micron Technology, Inc.",
            aliases=("micron technology", "micron technology, inc.", "mu"),
            source="integration_test",
        )
    )


def _publish_untagged_listicle(redis_client: redis.Redis, stream_name: str, article_id: str) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    redis_client.xadd(
        stream_name,
        {
            "event_id": f"evt-{article_id}",
            "event_type": "news.article.created",
            "event_version": "1.0",
            "occurred_at": "2026-06-15T12:00:00Z",
            "producer": "news_fetcher",
            "dedupe_key": article_id,
            "payload_json": json.dumps(
                {
                    "id": article_id,
                    "source": "integration",
                    "title": "The Best Stocks to Invest $1,000 in Right Now",
                    "summary": "Exploring some of the top bargains in a multi-trillion-dollar industry.",
                    "canonical_locator": "https://example.com/best-stocks-to-invest-1000-in-right-now",
                    "entities": [],
                    "occurred_at": now.isoformat(),
                    "ingested_at": now.isoformat(),
                    "attributes": {},
                }
            ),
        },
    )


def _rejection_reason_codes(settings: ThesisBuilderSettings) -> list[str]:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT rejection_reason_code FROM {settings.thesis_builder_db_schema}.t_news_analyses "
                f"ORDER BY analyzed_at"
            )
            return [row[0] for row in cur.fetchall()]


def _content_types(settings: ThesisBuilderSettings) -> list[str]:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT content_type FROM {settings.thesis_builder_db_schema}.t_news_analyses "
                f"ORDER BY analyzed_at"
            )
            return [row[0] for row in cur.fetchall()]


def _publish_news(redis_client: redis.Redis, stream_name: str, article_ids: list[str]) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for index, article_id in enumerate(article_ids, start=1):
        redis_client.xadd(
            stream_name,
            {
                "event_id": f"evt-{article_id}",
                "event_type": "news.article.created",
                "event_version": "1.0",
                "occurred_at": "2026-06-15T12:00:00Z",
                "producer": "news_fetcher",
                "dedupe_key": article_id,
                "payload_json": json.dumps(
                    {
                        "id": article_id,
                        "source": "integration",
                        "title": f"Apple raises guidance {index}",
                        "summary": "Apple raised revenue guidance.",
                        "canonical_locator": f"https://example.com/{article_id}",
                        "entities": ["AAPL"],
                        "occurred_at": now.isoformat(),
                        "ingested_at": now.isoformat(),
                        "attributes": {"sentiment_source": 0.8},
                    }
                ),
            },
        )


def _count(settings: ThesisBuilderSettings, table_name: str) -> int:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {settings.thesis_builder_db_schema}.{table_name}")
            return int(cur.fetchone()[0])


def _count_shared_reviews(settings: ThesisBuilderSettings) -> int:
    with psycopg.connect(**db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {settings.shared_db_schema}.t_thesis_card_reviews")
            return int(cur.fetchone()[0])
