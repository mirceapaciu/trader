from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

import psycopg
from psycopg.types.json import Json
import pytest
import redis

from src.product_components.monitoring_ui.backend.repository import (
    PostgresRedisMonitoringDataSource,
)
from src.product_components.thesis_builder.event_identity import (
    DEFAULT_TAXONOMY_SNAPSHOT,
    normalize_event_identity,
    renormalize_event_identity,
)
from src.product_components.thesis_builder.redis_io import RedisThesisBuilderIo
from src.product_components.thesis_builder.repository import (
    PostgresThesisBuilderRepository,
)
from src.product_components.thesis_builder.settings import ThesisBuilderSettings
from src.product_components.thesis_builder.taxonomy_decisions import (
    TaxonomyDecisionRequest,
)
from src.product_components.thesis_builder.taxonomy_gateway import (
    RedisTaxonomyCommandPublisher,
    ThesisTaxonomyDecisionGateway,
)
from src.product_components.thesis_builder.taxonomy_runtime import (
    EventTaxonomySnapshotProvider,
)
from src.product_components.thesis_builder.taxonomy_worker import (
    TaxonomyBackfillWorker,
    TaxonomyCommandWorker,
)
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
def _prepare_infrastructure() -> None:
    config = db_config()
    ensure_postgres_access(config)
    ensure_safe_test_database(config)
    ensure_test_database_exists(config)
    bootstrap_newsfetcher_schema(config)
    ensure_safe_test_redis(redis_config())


def test_registry_seeds_all_builtin_event_stages_for_monitoring_ui() -> None:
    settings = ThesisBuilderSettings.from_env()
    data_source = PostgresRedisMonitoringDataSource(
        dsn=settings.postgres_dsn,
        news_schema=settings.news_fetcher_db_schema,
        filter_quality_schema=os.getenv(
            "FILTER_QUALITY_DB_SCHEMA", "filter_quality_evaluator"
        ),
        thesis_builder_schema=settings.thesis_builder_db_schema,
        queue_url=settings.queue_url,
        news_raw_queue=settings.news_raw_queue,
        failed_messages_dlq=settings.failed_messages_dlq,
        query_timeout_seconds=5,
    )

    values = data_source.get_thesis_builder_taxonomy_values(
        dimension="event_stage",
        family_scope=None,
    )

    assert {value.canonical_value for value in values.values} >= {
        "proposed", "scheduled", "announced", "pending", "approved",
        "in_progress", "completed", "cancelled", "denied", "corrected",
        "unknown",
    }


def test_authenticated_command_activates_value_and_completes_bounded_backfill() -> None:
    settings = ThesisBuilderSettings.from_env()
    suffix = uuid.uuid4().hex
    proposal = f"runtime_stage_{suffix[:12]}"
    article_id = f"taxonomy-integration-{suffix}"
    command_stream = f"taxonomy_command_queue_test_{suffix}"
    consumer_group = f"taxonomy_group_test_{suffix}"
    repository = PostgresThesisBuilderRepository(
        dsn=settings.postgres_dsn,
        thesis_schema=settings.thesis_builder_db_schema,
    )
    redis_client = redis.from_url(settings.queue_url, decode_responses=True)

    with psycopg.connect(settings.postgres_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT taxonomy_revision FROM thesis_builder.t_event_taxonomy_state "
                "WHERE singleton = TRUE"
            )
            initial_revision = int(cur.fetchone()[0])
            raw = {
                "event_family": "partnership_joint_venture",
                "event_stage": proposal,
                "coverage_role": "primary_announcement",
                "subject": {"ticker": "E2E", "exchange_code": "XNAS"},
            }
            identity = normalize_event_identity(
                raw,
                ticker="E2E",
                exchange_code="XNAS",
                taxonomy=EventTaxonomySnapshotProvider(
                    source=repository,
                    baseline=DEFAULT_TAXONOMY_SNAPSHOT,
                ).get(initial_revision),
            )
            cur.execute(
                """
                INSERT INTO thesis_builder.t_news_analyses (
                    article_id, ticker, exchange_code, sentiment, relevance, urgency,
                    suggested_action, confidence, llm_model, analyzed_at,
                    event_identity_json, taxonomy_revision
                )
                VALUES (%s, 'E2E', 'XNAS', 0.5, 0.9, 'normal', 'hold', 0.9,
                        'taxonomy-integration', %s, %s, %s)
                RETURNING id
                """,
                (
                    article_id,
                    datetime.now(timezone.utc),
                    Json(identity),
                    initial_revision,
                ),
            )
            analysis_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO thesis_builder.t_event_taxonomy_gaps (
                    dimension, raw_value, normalized_proposal, occurrence_count,
                    first_seen_at, last_seen_at, representative_analysis_ids,
                    representative_headlines, status
                )
                VALUES ('event_stage', %s, %s, 1, NOW(), NOW(), %s, %s, 'open')
                RETURNING id
                """,
                (proposal, proposal, Json([analysis_id]), Json(["Integration taxonomy gap"])),
            )
            gap_id = int(cur.fetchone()[0])
        conn.commit()

    try:
        io = RedisThesisBuilderIo(
            queue_url=settings.queue_url,
            news_raw_queue=settings.news_raw_queue,
            signal_queue=settings.signal_queue,
            failed_messages_dlq=settings.failed_messages_dlq,
            consumer_group=consumer_group,
            consumer_name="taxonomy_integration_worker",
            reprocess_command_queue=settings.reprocess_command_queue,
            taxonomy_command_queue=command_stream,
            claim_min_idle_ms=0,
        )
        io.ensure_streams_and_group()
        gateway = ThesisTaxonomyDecisionGateway(
            repository=repository,
            command_publisher=RedisTaxonomyCommandPublisher(
                queue_url=settings.queue_url,
                command_stream=command_stream,
            ),
        )
        accepted = gateway.submit(
            request=TaxonomyDecisionRequest(
                gap_id=gap_id,
                expected_gap_status="open",
                action="accept_new",
                canonical_value=proposal,
                display_name="Runtime integration stage",
                description="Integration-only accepted stage.",
                family_scope=None,
                identity_discriminators=("channel=integration",),
                rationale="Exercise the durable command and backfill workflow.",
                idempotency_key=f"taxonomy-integration-{suffix}",
            ),
            actor="integration.operator@example.test",
        )
        assert accepted.status == "accepted"

        commands = io.read_taxonomy_commands(count=1, block_ms=100)
        assert len(commands) == 1
        TaxonomyCommandWorker(repository=repository).process(
            command_id=commands[0].command_id
        )
        io.ack_taxonomy(commands[0].message_id)

        provider = EventTaxonomySnapshotProvider(
            source=repository,
            baseline=DEFAULT_TAXONOMY_SNAPSHOT,
        )
        backfill = TaxonomyBackfillWorker(
            repository=repository,
            normalize=lambda stored, revision: renormalize_event_identity(
                stored,
                taxonomy=provider.get(revision),
            ),
            batch_size=1,
        )
        assert backfill.run_batch() is True
        assert backfill.run_batch() is True
        assert backfill.run_batch() is False

        completed = gateway.get(command_id=accepted.command_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.taxonomy_revision == initial_revision + 1
        assert completed.backfill is not None
        assert completed.backfill.status == "completed"
        assert completed.backfill.changed_count == 1
        assert completed.backfill.failed_count == 0

        with psycopg.connect(settings.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_identity_json, taxonomy_revision "
                    "FROM thesis_builder.t_news_analyses WHERE id = %s",
                    (analysis_id,),
                )
                stored_identity, stored_revision = cur.fetchone()
                cur.execute(
                    "SELECT actor FROM thesis_builder.t_event_taxonomy_decisions "
                    "WHERE gap_id = %s",
                    (gap_id,),
                )
                actor = cur.fetchone()[0]
        assert stored_identity["event_stage"] == proposal
        assert stored_identity["event_stage_candidate"] is None
        assert stored_revision == initial_revision + 1
        assert actor == "integration.operator@example.test"

        data_source = PostgresRedisMonitoringDataSource(
            dsn=settings.postgres_dsn,
            news_schema=settings.news_fetcher_db_schema,
            filter_quality_schema=os.getenv(
                "FILTER_QUALITY_DB_SCHEMA", "filter_quality_evaluator"
            ),
            thesis_builder_schema=settings.thesis_builder_db_schema,
            queue_url=settings.queue_url,
            news_raw_queue=settings.news_raw_queue,
            failed_messages_dlq=settings.failed_messages_dlq,
            query_timeout_seconds=5,
        )
        values = data_source.get_thesis_builder_taxonomy_values(
            dimension="event_stage",
            family_scope=None,
        )
        assert any(value.canonical_value == proposal for value in values.values)
        assert values.taxonomy_revision == initial_revision + 1
        assert sum(
            int(group.get("pending") or 0)
            for group in redis_client.xinfo_groups(command_stream)
        ) == 0
    finally:
        with psycopg.connect(settings.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM thesis_builder.t_event_taxonomy_commands "
                    "WHERE gap_id = %s",
                    (gap_id,),
                )
                cur.execute(
                    "DELETE FROM thesis_builder.t_event_taxonomy_backfill_jobs "
                    "WHERE decision_id IN ("
                    "SELECT id FROM thesis_builder.t_event_taxonomy_decisions "
                    "WHERE gap_id = %s)",
                    (gap_id,),
                )
                cur.execute(
                    "DELETE FROM thesis_builder.t_event_taxonomy_decisions "
                    "WHERE gap_id = %s",
                    (gap_id,),
                )
                cur.execute(
                    "DELETE FROM thesis_builder.t_event_taxonomy_gaps WHERE id = %s",
                    (gap_id,),
                )
                cur.execute(
                    "DELETE FROM thesis_builder.t_event_taxonomy_values "
                    "WHERE dimension = 'event_stage' AND canonical_value = %s",
                    (proposal,),
                )
                cur.execute(
                    "DELETE FROM thesis_builder.t_news_analyses WHERE id = %s",
                    (analysis_id,),
                )
                cur.execute(
                    "UPDATE thesis_builder.t_event_taxonomy_state "
                    "SET taxonomy_revision = %s WHERE singleton = TRUE",
                    (initial_revision,),
                )
            conn.commit()
        redis_client.delete(command_stream)
