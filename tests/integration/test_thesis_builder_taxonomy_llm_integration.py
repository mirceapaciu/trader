"""Opt-in real-model evaluation for ThesisBuilder event-taxonomy selection.

This suite intentionally makes paid API calls only when RUN_LLM_INTEGRATION=1.
It uses committed, de-identified inputs and never loads application service settings.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
import redis

from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.thesis_builder.llm_client import OpenAIThesisClient, ThesisAnalyzer
from src.product_components.thesis_builder.models import NewsArticle
from src.product_components.thesis_builder.redis_io import RedisThesisBuilderIo
from src.product_components.thesis_builder.repository import PostgresThesisBuilderRepository
from src.product_components.thesis_builder.service import ThesisBuilderRunner
from src.product_components.thesis_builder.taxonomy_seed import predefined_taxonomy_values
from src.product_components.thesis_builder.taxonomy_runtime import EventTaxonomySnapshot, TaxonomyValue, build_taxonomy_snapshot
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    PostgresSharedThesisCardReviewWriter,
    SharedWatchlistEntryInput,
)
from tests.integration._db_test_helper import (
    bootstrap_newsfetcher_schema,
    db_config,
    ensure_postgres_access,
    ensure_safe_test_database,
    ensure_test_database_exists,
)
from tests.integration._redis_test_helper import ensure_safe_test_redis, redis_config


pytestmark = [pytest.mark.integration, pytest.mark.llm_eval]

_ROOT = Path(__file__).resolve().parents[2]
_CASES_PATH = _ROOT / "tests" / "product_components" / "thesis_builder" / "fixtures" / "taxonomy_llm_integration_cases.jsonl"
_RUN_FLAG = "RUN_LLM_INTEGRATION"
_MODEL_ENV = "THESIS_BUILDER_LLM_INTEGRATION_MODEL"
_DEFAULT_MODEL = "gpt-4o-mini-2024-07-18"


@pytest.fixture(scope="session")
def _live_config() -> tuple[str, str]:
    if os.getenv(_RUN_FLAG) != "1":
        pytest.skip(f"set {_RUN_FLAG}=1 to run paid real-LLM integration tests")
    # Deliberately do not load shared or production service env files here.
    load_env_files(_ROOT, filenames=(".env.secrets",), override_existing=False)
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        pytest.fail(f"{_RUN_FLAG}=1 requires OPENAI_API_KEY (loaded from .env.secrets or environment)")
    return api_key, os.getenv(_MODEL_ENV, _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


@pytest.fixture(scope="session")
def _taxonomy() -> EventTaxonomySnapshot:
    # A runtime-only value proves the real application prompt receives the selected
    # snapshot, rather than a hard-coded vocabulary. No model output is expected to use it.
    return build_taxonomy_snapshot(
        revision=2,
        values=(
            *predefined_taxonomy_values(),
            TaxonomyValue("event_family", "test_only_runtime_event", "active"),
        ),
    )


@pytest.fixture(scope="session")
def _analyzer(_live_config: tuple[str, str], _taxonomy: EventTaxonomySnapshot) -> ThesisAnalyzer:
    api_key, model = _live_config
    return ThesisAnalyzer(
        # Retry transient transport/server failures, but let deterministic
        # schema and response-validation failures abort this paid evaluation.
        client=OpenAIThesisClient(api_key=api_key, request_timeout_seconds=60, max_retries=2),
        model=model,
        max_tokens_per_run=100_000,
        max_tokens_per_item=6_000,
        taxonomy_revision=_taxonomy.revision,
        taxonomy_snapshot_provider=_StaticTaxonomyProvider(_taxonomy),
    )


def test_taxonomy_corpus_uses_real_application_analysis_path(
    _analyzer: ThesisAnalyzer, _taxonomy: EventTaxonomySnapshot, _live_config: tuple[str, str]
) -> None:
    _, model = _live_config
    results: list[dict[str, object]] = []
    required_fields = 0
    matched_fields = 0
    synonym_baseline = 0
    synonym_remaining = 0

    for case in _load_cases():
        started = time.monotonic()
        article = _article(case)
        result = _analyzer.analyze_article(
            article=article, ticker=case["ticker"], exchange_code=case["exchange_code"],
        )
        identity = result.event_identity
        expected = case["expected"]
        reasons = _assert_hard_safety(case, identity, result.estimated_tokens, _taxonomy)
        for field, expected_value in expected.items():
            if field.endswith("_any") or field in {"no_candidates", "event_family_candidate_required"}:
                continue
            required_fields += 1
            if identity.get(field) == expected_value:
                matched_fields += 1
            else:
                reasons.append(f"{field}={identity.get(field)!r}, expected {expected_value!r}")
        for field, alternatives in expected.items():
            if not field.endswith("_any"):
                continue
            identity_field = field.removesuffix("_any")
            required_fields += 1
            if identity.get(identity_field) in alternatives:
                matched_fields += 1
            else:
                reasons.append(f"{identity_field}={identity.get(identity_field)!r}, expected one of {alternatives!r}")
        for dimension in expected.get("no_candidates", []):
            baseline_value = case.get("baseline_candidates", {}).get(dimension)
            synonym_baseline += int(bool(baseline_value))
            actual = identity.get(f"{dimension}_candidate")
            synonym_remaining += int(bool(actual))
            if actual:
                reasons.append(f"unexpected {dimension}_candidate={actual!r}")
        results.append(_artifact_row(case, model, _taxonomy.revision, identity, result.estimated_tokens, time.monotonic() - started, reasons))

    _write_artifact(results)
    assert required_fields and matched_fields / required_fields >= 0.90, results
    assert synonym_baseline == 0 or (synonym_baseline - synonym_remaining) / synonym_baseline >= 0.90, results
    assert not [row for row in results if row["reasons"]], results


def test_runner_persists_canonical_and_novel_taxonomy_gap_smoke(
    _live_config: tuple[str, str], _analyzer: ThesisAnalyzer
) -> None:
    """Exercise the normal Redis -> runner -> repository path in disposable infra."""
    from tests.integration.test_thesis_builder_integration import _cleanup, _settings

    api_key, model = _live_config
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    bootstrap_newsfetcher_schema(config)
    ensure_safe_test_redis(redis_config())
    settings = replace(_settings(), llm_model=model, openai_api_key=api_key, llm_max_output_tokens=6_000)
    client = redis.Redis(
        host=str(redis_config()["host"]), port=int(redis_config()["port"]), db=int(redis_config()["db"]),
        password=os.getenv("REDIS_PASSWORD") or None, decode_responses=True,
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis is not reachable for LLM smoke test: {exc}")
    _cleanup(settings, client)
    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_event_taxonomy_commands")
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_event_taxonomy_decisions")
            cur.execute(f"DELETE FROM {settings.thesis_builder_db_schema}.t_event_taxonomy_gaps")
        conn.commit()
    for ticker, exchange, name in (("MSFT", "XNAS", "Microsoft"), ("ORBT", "XNAS", "Orbital Systems")):
        PostgresSharedInstrumentAdmin(dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema).upsert_watchlist_entry(
            SharedWatchlistEntryInput(ticker=ticker, exchange_code=exchange, display_name=name, source="llm_integration_test")
        )
    runner = ThesisBuilderRunner(
        settings=settings,
        repository=PostgresThesisBuilderRepository(dsn=settings.postgres_dsn, thesis_schema=settings.thesis_builder_db_schema),
        redis_io=RedisThesisBuilderIo(queue_url=settings.queue_url, news_raw_queue=settings.news_raw_queue, signal_queue=settings.signal_queue, failed_messages_dlq=settings.failed_messages_dlq, consumer_group=settings.consumer_group, consumer_name=settings.consumer_name),
        analyzer=_analyzer,
        instrument_registry=PostgresSharedInstrumentRegistry(dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema, watchlist_table="t_watchlist_tickers"),
        review_writer=PostgresSharedThesisCardReviewWriter(dsn=settings.postgres_dsn, shared_schema=settings.shared_db_schema),
    )
    cases = {case["id"]: case for case in _load_cases()}
    _publish_case(client, settings.news_raw_queue, cases["msft-earnings-preview"])
    _publish_case(client, settings.news_raw_queue, cases["synthetic-novel-orbital-license"])
    runner.bootstrap()
    assert runner.run_once() == 2
    with psycopg.connect(**config) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT event_identity_json FROM {settings.thesis_builder_db_schema}.t_news_analyses ORDER BY id")
            identities = [row[0] for row in cur.fetchall()]
            cur.execute(f"SELECT dimension, normalized_proposal FROM {settings.thesis_builder_db_schema}.t_event_taxonomy_gaps ORDER BY id")
            gaps = cur.fetchall()
    assert len(identities) == 2
    assert any(identity["event_family"] == "earnings_results" and not identity["event_family_candidate"] for identity in identities)
    assert any(identity["event_family_candidate"] for identity in identities)
    assert len(gaps) == 1
    assert gaps[0][0] == "event_family" and gaps[0][1]


def _publish_case(client: redis.Redis, stream: str, case: dict[str, object]) -> None:
    article = _article(case)
    client.xadd(stream, {
        "event_id": f"llm-eval-{article.id}", "event_type": "news.article.created", "event_version": "1.0",
        "occurred_at": article.published_at.isoformat(), "producer": "llm_integration_test", "dedupe_key": article.id,
        "payload_json": json.dumps({"id": article.id, "source": article.source, "title": article.headline,
            "summary": article.summary, "canonical_locator": article.url, "entities": article.tickers,
            "occurred_at": article.published_at.isoformat(), "ingested_at": article.fetched_at.isoformat(), "attributes": {}}),
    })


def _assert_hard_safety(
    case: dict[str, object], identity: dict[str, object], tokens: int, taxonomy: EventTaxonomySnapshot
) -> list[str]:
    reasons: list[str] = []
    expected = case["expected"]
    family = identity.get("event_family")
    subtype = identity.get("event_subtype")
    if family is not None and family not in taxonomy.canonical_values["event_family"]:
        reasons.append("unknown canonical event_family")
    if subtype is not None and taxonomy.resolve("event_subtype", subtype, family_scope=family) != subtype:
        reasons.append("invalid family-scoped subtype")
    if tokens <= 0:
        reasons.append("missing actual token usage")
    if expected.get("event_family_candidate_required") and not identity.get("event_family_candidate"):
        reasons.append("genuine novel event family candidate was lost")
    return reasons


def _load_cases() -> list[dict[str, object]]:
    return [json.loads(line) for line in _CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _article(case: dict[str, object]) -> NewsArticle:
    published_at = datetime.fromisoformat(str(case["published_at"]))
    return NewsArticle(
        id=str(case["id"]), source="taxonomy_llm_integration", headline=str(case["headline"]),
        summary=str(case["summary"]), url=f"https://fixture.invalid/{case['id']}",
        tickers=[str(case["ticker"])], published_at=published_at, fetched_at=published_at,
    )


def _artifact_row(case, model, revision, identity, tokens, latency, reasons) -> dict[str, object]:
    return {
        "fixture_id": case["id"], "model": model, "taxonomy_revision": revision,
        "expected": case["expected"],
        "actual": {key: identity.get(key) for key in ("event_family", "event_subtype", "event_stage", "coverage_role", "event_family_candidate", "event_subtype_candidate")},
        "token_count": tokens, "latency_seconds": round(latency, 3), "reasons": reasons,
    }


def _write_artifact(rows: list[dict[str, object]]) -> None:
    output = Path(os.getenv("THESIS_BUILDER_LLM_INTEGRATION_ARTIFACT", _ROOT / "temp" / "taxonomy-llm-integration-results.jsonl"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class _StaticTaxonomyProvider:
    def __init__(self, taxonomy: EventTaxonomySnapshot) -> None:
        self._taxonomy = taxonomy

    def get(self, taxonomy_revision: int | None = None) -> EventTaxonomySnapshot:
        if taxonomy_revision not in (None, self._taxonomy.revision):
            raise ValueError("unexpected taxonomy revision")
        return self._taxonomy
