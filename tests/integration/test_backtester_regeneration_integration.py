from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import redis
from psycopg.types.json import Json

from src.core_components.backtest_engine import Bar
from src.product_components.backtester.models import BacktestMode, BacktestRunParams, TimingScenario
from src.product_components.backtester.regeneration import ThesisRegenerationProvider
from src.product_components.backtester.repository import BacktesterRepository, sim_schema_name
from src.product_components.backtester.service import BacktesterService, new_run_id
from src.product_components.backtester.settings import BacktesterSettings
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    SharedWatchlistEntryInput,
)
from src.product_components.thesis_builder.export import ThesisCardHistoryExporter
from src.product_components.thesis_builder.models import ContentType, ThesisStrategy, TradeDirection
from src.product_components.thesis_builder.settings import ThesisBuilderSettings
from tests.integration._db_test_helper import (
    db_config,
    ensure_postgres_access,
    ensure_safe_test_database,
    ensure_test_database_exists,
)
from tests.integration._redis_test_helper import ensure_safe_test_redis, redis_config

pytestmark = pytest.mark.integration

_BASE = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def _prepare() -> None:
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    ensure_safe_test_redis(redis_config())


# ----- fake collaborators (no OpenAI, no market-data network) ------------


class _FakeLlmClient:
    """Deterministic ThesisBuilder LLM client — returns a valid executable analysis."""

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        payload = json.loads(prompt)
        return {
            "ticker": payload["instrument"]["ticker"],
            "exchange_code": payload["instrument"]["exchange_code"],
            "sentiment": 0.8,
            "relevance": 0.9,
            "urgency": "today",
            "suggested_action": "buy",
            "candidate_strategy": ThesisStrategy.SENTIMENT_MOMENTUM.value,
            "direction": TradeDirection.BUY.value,
            "confidence": 0.85,
            "reasoning": "bullish catalyst",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "content_type": ContentType.NEWS_CATALYST.value,
            "event_type": "guidance",
            "price_impact_magnitude": "medium",
            "evidence_bullet_candidates": [payload["article"]["headline"]],
            "estimated_tokens": 100,
        }


class _NoMarketData:
    """Stands in for MarketDataService: no historical daily bars -> context is None."""

    def get_historical_bars(self, *, ticker, exchange_code, interval, start, end):
        return []


class _RisingBars:
    """Engine bars: a steadily rising series from the requested entry time."""

    def historical_bars(self, *, ticker, exchange_code, interval, start, end):
        bars = []
        price = 100.0
        for i in range(120):
            nxt = price * 1.003
            bars.append(
                Bar(start_at=start + timedelta(minutes=i), open=price, high=nxt, low=price, close=nxt, volume=1000)
            )
            price = nxt
        return bars

    def warm(self, instruments, *, interval, start, end, progress=None):
        return None


# ----- test --------------------------------------------------------------


def test_regeneration_isolates_data_and_produces_backtest_results() -> None:
    config = db_config()
    dsn = _dsn(config)
    bt_settings = dataclasses.replace(
        BacktesterSettings.from_env(),
        regeneration_enabled=True,
        persist_card_snapshots=True,
        persist_equity_points=True,
    )
    thesis_settings = ThesisBuilderSettings.from_env()
    redis_client = _redis_client()
    _wait_for_redis(redis_client)

    repository = BacktesterRepository(
        dsn=dsn,
        backtester_schema=bt_settings.db_schema,
        market_data_schema=bt_settings.market_data_db_schema,
        thesis_builder_schema=bt_settings.thesis_builder_db_schema,
        shared_schema=bt_settings.shared_db_schema,
    )
    repository.bootstrap_schema(repo_root=_repo_root())

    run_id = new_run_id()
    sim_schema = sim_schema_name(run_id)
    _cleanup(dsn, bt_settings, redis_client, sim_schema, thesis_settings.signal_queue)
    _seed_instrument(thesis_settings)
    # required_evidence_count defaults to 3, and a card needs >= 2 distinct articles.
    _seed_articles(dsn, thesis_settings, count=thesis_settings.required_evidence_count)

    instrument_registry = PostgresSharedInstrumentRegistry(
        dsn=dsn, shared_schema=bt_settings.shared_db_schema, watchlist_table="t_watchlist_tickers"
    )
    provider = ThesisRegenerationProvider(
        dsn=dsn,
        thesis_settings=thesis_settings,
        instrument_registry=instrument_registry,
        market_data_service=_NoMarketData(),
        quote_max_age_seconds=300,
        llm_client_factory=_FakeLlmClient,
    )

    signal_len_before = redis_client.xlen(thesis_settings.signal_queue)

    params = BacktestRunParams(
        run_id=run_id,
        window_start_at=_BASE - timedelta(hours=1),
        window_end_at=_BASE + timedelta(hours=8),
        mode=BacktestMode.REGENERATION,
        timing_scenario=TimingScenario.IDEAL,
        llm_model="fake-model-x",
        llm_max_tokens_per_run=100000,
    )

    BacktesterService(
        settings=bt_settings,
        repository=repository,
        cards_provider=ThesisCardHistoryExporter(dsn=dsn, thesis_schema=bt_settings.thesis_builder_db_schema),
        bars_provider=_RisingBars(),
        regeneration_provider=provider,
        repo_root=_repo_root(),
    ).run(params)

    # --- production data is untouched ---
    assert _count(dsn, f"{bt_settings.thesis_builder_db_schema}.t_news_analyses") == 0
    assert _count(dsn, f"{bt_settings.thesis_builder_db_schema}.t_thesis_cards") == 0
    # --- nothing published to the live queue ---
    assert redis_client.xlen(thesis_settings.signal_queue) == signal_len_before

    # --- regenerated data lives in the isolated sim schema ---
    assert _schema_exists(dsn, sim_schema)
    assert _count(dsn, f"{sim_schema}.t_news_analyses") == thesis_settings.required_evidence_count
    assert _count(dsn, f"{sim_schema}.t_thesis_cards") == 1

    # --- backtester recorded a completed run with the chosen model + a simulated trade ---
    run_row = _run_row(dsn, bt_settings.db_schema, run_id)
    assert run_row["status"] == "completed"
    assert run_row["mode"] == "regeneration"
    assert run_row["llm_model"] == "fake-model-x"
    assert run_row["llm_token_budget_limit"] == 100000
    assert run_row["thesis_config_snapshot_json"] is not None
    assert _count(dsn, f"{bt_settings.db_schema}.t_backtest_trades", where=f"run_id = '{run_id}'") >= 1

    repository.drop_sim_thesis_schema(sim_schema=sim_schema)


# ----- helpers -----------------------------------------------------------


def _dsn(config: dict[str, object]) -> str:
    return (
        f"host={config['host']} port={config['port']} dbname={config['dbname']} "
        f"user={config['user']} password={config['password']} sslmode={config['sslmode']}"
    )


def _seed_instrument(settings: ThesisBuilderSettings) -> None:
    PostgresSharedInstrumentAdmin(
        dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema
    ).upsert_watchlist_entry(
        SharedWatchlistEntryInput(
            ticker="AAPL", exchange_code="XNAS", display_name="Apple Inc.", source="integration_test"
        )
    )


def _seed_articles(dsn: str, settings: ThesisBuilderSettings, *, count: int) -> None:
    sql = (
        f"INSERT INTO {settings.news_fetcher_db_schema}.t_news_articles "
        f"(id, source, source_key, headline, summary, url, tickers, published_at, fetched_at, sentiment_source) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for i in range(count):
            published = _BASE + timedelta(minutes=5 * i)
            cur.execute(
                sql,
                (
                    f"regen-art-{i}",
                    "integration",
                    f"regen-key-{i}",
                    f"Apple beats expectations {i}",
                    "Apple reported strong quarterly guidance.",
                    f"https://example.com/regen/{i}",
                    Json(["AAPL"]),
                    published,
                    published,
                    0.8,
                ),
            )
        conn.commit()


def _cleanup(dsn: str, settings: BacktesterSettings, redis_client: redis.Redis, sim_schema: str,
             signal_queue: str) -> None:
    redis_client.delete(signal_queue)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {sim_schema} CASCADE")
        for table in (
            f"{settings.db_schema}.t_backtest_trades",
            f"{settings.db_schema}.t_backtest_equity_points",
            f"{settings.db_schema}.t_backtest_card_snapshots",
            f"{settings.db_schema}.t_backtest_runs",
            f"{settings.thesis_builder_db_schema}.t_thesis_cards",
            f"{settings.thesis_builder_db_schema}.t_evidence_windows",
            f"{settings.thesis_builder_db_schema}.t_news_analyses",
            f"{settings.shared_db_schema}.t_watchlist_tickers",
            "news_fetcher.t_news_articles",
        ):
            cur.execute(f"DELETE FROM {table}")
        conn.commit()


def _count(dsn: str, table: str, *, where: str | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return int(cur.fetchone()[0])


def _schema_exists(dsn: str, schema: str) -> bool:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
        return cur.fetchone() is not None


def _run_row(dsn: str, schema: str, run_id: str) -> dict:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT status, mode, llm_model, llm_token_budget_limit, thesis_config_snapshot_json "
            f"FROM {schema}.t_backtest_runs WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    assert row is not None, "backtest run row missing"
    return {
        "status": row[0],
        "mode": row[1],
        "llm_model": row[2],
        "llm_token_budget_limit": row[3],
        "thesis_config_snapshot_json": row[4],
    }


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


def _wait_for_redis(client: redis.Redis) -> None:
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Redis is not reachable for integration test: {exc}")


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]
