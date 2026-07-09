from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg import errors
import redis
from psycopg.rows import dict_row
from psycopg.types.json import Json
from src.product_components.filter_quality_evaluator.keyword_recommendations import sanitize_suggestion_json
from src.product_components.news_fetcher.filter_config import (
    NewsFilterConfig,
    config_from_snapshot,
    new_filter_config_id,
    normalize_keywords,
    normalize_tickers,
)
from src.product_components.news_fetcher.settings import NewsFetcherSettings

from .models import (
    BacklogResponse,
    DeadLetterItem,
    DeadLetterResponse,
    DependencyHealth,
    FetchedArticle,
    FetchedArticlesResponse,
    FilterQualityIncorrectlyAcceptedItem,
    FilterQualityIncorrectlyAcceptedResponse,
    FilterQualityIncorrectlyRejectedItem,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityRunSummary,
    FilterQualityStatusResponse,
    NewsAnalysesResponse,
    NewsAnalysisItem,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ProviderStatus,
    ThesisBuilderActionableCard,
    ThesisBuilderConsumerHealth,
    ThesisBuilderThroughputBucket,
    ThesisBuilderThroughputResponse,
    ThesisBuilderMetricsResponse,
    ThesisBuilderDeadLetterItem,
    ThesisBuilderEvidenceWindow,
    ThesisCardSummary,
    ThroughputBucket,
    ThroughputResponse,
    WindowArticle,
    WindowArticlesResponse,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_THESIS_BUILDER_RECENT_DEAD_LETTER_LIMIT = 10
_DLQ_SCAN_BATCH_SIZE = 500

_BACKTEST_RUN_COLUMNS = (
    "run_id, status, window_start_at, window_end_at, mode, timing_scenario, card_population, "
    "strategies_requested, initial_capital, net_pnl, total_return, win_rate, profit_factor, "
    "max_drawdown, created_at, started_at, finished_at, error_code, gross_profit, gross_loss, "
    "total_commission, total_slippage, avg_win, avg_loss, expectancy, "
    "max_drawdown_duration_seconds, sharpe_ratio, exposure_fraction, signal_accuracy, "
    "cards_considered, cards_in_population, cards_live_executable, cards_skipped_no_price, "
    "trades_opened, trades_closed, trades_risk_blocked, "
    "avg_news_fetch_delay_seconds, p95_news_fetch_delay_seconds, max_news_fetch_delay_seconds, "
    "avg_thesis_build_delay_seconds, p95_thesis_build_delay_seconds, max_thesis_build_delay_seconds, "
    "avg_total_pipeline_delay_seconds, p95_total_pipeline_delay_seconds, max_total_pipeline_delay_seconds, "
    "pnl_gap, win_rate_gap, trades_flipped_by_delay, llm_model, "
    "llm_token_budget_limit, llm_tokens_used, budget_exhausted, analysis_coverage_until_at, "
    "summary_json"
)


class BacktesterTablesUnavailable(RuntimeError):
    """Raised when the backtester schema/tables are missing, so the UI can degrade."""


class PostgresRedisMonitoringDataSource:
    def __init__(
        self,
        *,
        dsn: str,
        news_schema: str,
        filter_quality_schema: str,
        thesis_builder_schema: str,
        queue_url: str,
        news_raw_queue: str,
        failed_messages_dlq: str,
        query_timeout_seconds: int,
        backtester_schema: str = "backtester",
    ) -> None:
        self._dsn = dsn
        self._news_schema = _safe_identifier(news_schema)
        self._filter_quality_schema = _safe_identifier(filter_quality_schema)
        self._thesis_builder_schema = _safe_identifier(thesis_builder_schema)
        self._backtester_schema = _safe_identifier(backtester_schema)
        self._queue_url = queue_url
        self._news_raw_queue = news_raw_queue
        self._failed_messages_dlq = failed_messages_dlq
        self._query_timeout_seconds = query_timeout_seconds
        self._redis = redis.from_url(
            self._queue_url,
            decode_responses=True,
            socket_connect_timeout=self._query_timeout_seconds,
            socket_timeout=self._query_timeout_seconds,
        )

    def check_dependencies(self) -> list[DependencyHealth]:
        checked_at = _utc_now()
        dependencies: list[DependencyHealth] = []

        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            dependencies.append(
                DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=checked_at)
            )
        except Exception as exc:
            dependencies.append(
                DependencyHealth(
                    name="postgres",
                    kind="postgres",
                    state="unhealthy",
                    message=str(exc),
                    checked_at=checked_at,
                )
            )

        try:
            client = redis.from_url(
                self._queue_url,
                decode_responses=True,
                socket_connect_timeout=self._query_timeout_seconds,
                socket_timeout=self._query_timeout_seconds,
            )
            client.ping()
            dependencies.append(DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=checked_at))
        except Exception as exc:
            dependencies.append(
                DependencyHealth(name="redis", kind="redis", state="unhealthy", message=str(exc), checked_at=checked_at)
            )

        return dependencies

    def bootstrap_news_schema(self, *, repo_root: Path) -> None:
        schema_file = repo_root / "src" / "product_components" / "news_fetcher" / "db" / "schema.sql"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(schema_file.read_text(encoding="utf-8"))

    def bootstrap_shared_schema(self, *, repo_root: Path) -> None:
        schema_file = repo_root / "src" / "product_components" / "shared" / "db" / "schema.sql"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(schema_file.read_text(encoding="utf-8"))

    def bootstrap_thesis_builder_schema(self, *, repo_root: Path) -> None:
        schema_file = repo_root / "src" / "product_components" / "thesis_builder" / "db" / "schema.sql"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(schema_file.read_text(encoding="utf-8"))

    def list_providers(self) -> ProvidersResponse:
        sql = (
            f"SELECT source_key, last_cycle_started_at, last_cycle_finished_at, last_fetch_attempt_at, "
            f"last_non_zero_fetch_at, "
            f"last_cycle_fetched_count, last_cycle_accepted_count, last_cycle_rejected_count, "
            f"last_cycle_error_code "
            f"FROM {self._news_schema}.t_provider_cycle_status "
            f"ORDER BY source_key"
        )
        providers: list[ProviderStatus] = []
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        for row in rows:
            last_cycle_start = _to_utc(row["last_cycle_started_at"])
            last_cycle_end = _to_utc(row["last_cycle_finished_at"])
            providers.append(
                ProviderStatus(
                    source_key=row["source_key"],
                    last_cycle_end_at=last_cycle_end,
                    last_cycle_start_at=last_cycle_start,
                    last_cycle_duration_seconds=max(
                        0.0,
                        (last_cycle_end - last_cycle_start).total_seconds(),
                    ),
                    last_fetch_attempt_at=_to_utc(row["last_fetch_attempt_at"])
                    if row["last_fetch_attempt_at"]
                    else None,
                    last_non_zero_fetch_at=_to_utc(row["last_non_zero_fetch_at"])
                    if row["last_non_zero_fetch_at"]
                    else None,
                    publish_success_count=self._count_published(row["source_key"]),
                    fetch_count=int(row["last_cycle_fetched_count"]),
                    fetch_error_count=1 if row["last_cycle_error_code"] else 0,
                    persist_success_count=int(row["last_cycle_accepted_count"]),
                    last_error_code=row["last_cycle_error_code"],
                    last_error_at=last_cycle_end if row["last_cycle_error_code"] else None,
                )
            )

        return ProvidersResponse(providers=providers, generated_at=_utc_now())

    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        resolved_end_at = _to_utc(end_at) if end_at is not None else _utc_now()
        resolved_start_at = (
            _to_utc(start_at) if start_at is not None else resolved_end_at - _window_to_timedelta(window)
        )
        granularity = _throughput_granularity_for_window(window)
        bucket_expression = _throughput_bucket_expression(window)
        sql = (
            f"SELECT {bucket_expression} AS window_start, source_key, "
            f"COUNT(*) FILTER (WHERE status IN ('pending', 'publishing', 'published', 'dead_lettered')) AS fetch_count, "
            f"COUNT(*) FILTER (WHERE status = 'published') AS publish_success_count, "
            f"COUNT(*) FILTER (WHERE status = 'dead_lettered') AS publish_error_count "
            f"FROM {self._news_schema}.t_publication_obligations "
            f"WHERE created_at >= %s AND created_at < %s "
            f"GROUP BY 1, 2 ORDER BY 1, 2"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (resolved_start_at, resolved_end_at))
            rows = cur.fetchall()

        buckets = [
            ThroughputBucket(
                window_start=_to_utc(row["window_start"]),
                source_key=row["source_key"],
                fetch_count=int(row["fetch_count"]),
                publish_success_count=int(row["publish_success_count"]),
                publish_error_count=int(row["publish_error_count"]),
            )
            for row in rows
        ]
        return ThroughputResponse(
            window=window,
            granularity=granularity,
            window_start_at=resolved_start_at,
            window_end_at=resolved_end_at,
            buckets=buckets,
            generated_at=_utc_now(),
        )

    def get_thesis_builder_throughput(
        self,
        *,
        window: str,
    ) -> ThesisBuilderThroughputResponse:
        resolved_end_at = _utc_now()
        resolved_start_at = resolved_end_at - _window_to_timedelta(window)
        granularity = _throughput_granularity_for_window(window)
        bucket_expression = _analysis_throughput_bucket_expression(window)
        generated_at = _utc_now()
        empty_response = ThesisBuilderThroughputResponse(
            available=False,
            message="ThesisBuilder throughput is unavailable.",
            window=window,
            granularity=granularity,
            window_start_at=resolved_start_at,
            window_end_at=resolved_end_at,
            buckets=[],
            generated_at=generated_at,
        )
        sql = (
            f"WITH analyses AS ("
            f"SELECT {bucket_expression} AS window_start, "
            f"COUNT(DISTINCT article_id) AS processed_articles_count, "
            f"COUNT(DISTINCT article_id) FILTER (WHERE content_type = 'news_catalyst') "
            f"AS news_catalyst_articles_count, "
            f"COUNT(DISTINCT article_id) FILTER (WHERE is_market_moving = TRUE) "
            f"AS market_moving_articles_count "
            f"FROM {self._thesis_builder_schema}.t_news_analyses "
            f"WHERE analyzed_at >= %s AND analyzed_at < %s "
            f"GROUP BY 1"
            f"), events AS ("
            f"SELECT {_message_event_bucket_expression(window)} AS window_start, "
            f"COUNT(*) AS consumed_messages_count, "
            f"COUNT(*) FILTER (WHERE outcome = 'skipped') AS skipped_messages_count, "
            f"COUNT(*) FILTER (WHERE outcome = 'failed_dlq') AS failed_messages_count "
            f"FROM {self._thesis_builder_schema}.t_message_processing_events "
            f"WHERE processed_at >= %s AND processed_at < %s "
            f"GROUP BY 1"
            f") "
            f"SELECT COALESCE(events.window_start, analyses.window_start) AS window_start, "
            f"COALESCE(events.consumed_messages_count, 0) AS consumed_messages_count, "
            f"COALESCE(events.skipped_messages_count, 0) AS skipped_messages_count, "
            f"COALESCE(events.failed_messages_count, 0) AS failed_messages_count, "
            f"COALESCE(analyses.processed_articles_count, 0) AS processed_articles_count, "
            f"COALESCE(analyses.news_catalyst_articles_count, 0) AS news_catalyst_articles_count, "
            f"COALESCE(analyses.market_moving_articles_count, 0) AS market_moving_articles_count "
            f"FROM events FULL OUTER JOIN analyses USING (window_start) "
            f"ORDER BY 1"
        )
        try:
            with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    sql,
                    (resolved_start_at, resolved_end_at, resolved_start_at, resolved_end_at),
                )
                rows = cur.fetchall()
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            return empty_response

        buckets = [
            ThesisBuilderThroughputBucket(
                window_start=_to_utc(row["window_start"]),
                consumed_messages_count=int(row["consumed_messages_count"]),
                skipped_messages_count=int(row["skipped_messages_count"]),
                failed_messages_count=int(row["failed_messages_count"]),
                processed_articles_count=int(row["processed_articles_count"]),
                news_catalyst_articles_count=int(row["news_catalyst_articles_count"]),
                market_moving_articles_count=int(row["market_moving_articles_count"]),
            )
            for row in rows
        ]
        return ThesisBuilderThroughputResponse(
            window=window,
            granularity=granularity,
            window_start_at=resolved_start_at,
            window_end_at=resolved_end_at,
            buckets=buckets,
            generated_at=generated_at,
        )

    def get_thesis_builder_metrics(
        self,
        *,
        window: str,
        evidence_collection_max_minutes: int,
    ) -> ThesisBuilderMetricsResponse:
        resolved_end_at = _utc_now()
        resolved_start_at = resolved_end_at - _window_to_timedelta(window)
        schema = self._thesis_builder_schema
        generated_at = _utc_now()
        empty_response = ThesisBuilderMetricsResponse(
            available=False,
            message="ThesisBuilder telemetry is unavailable.",
            window=window,
            window_start_at=resolved_start_at,
            window_end_at=resolved_end_at,
            articles_processed_count=0,
            market_moving_articles_count=0,
            articles_included_in_cards_count=0,
            stale_articles_count=0,
            created_thesis_cards_count=0,
            pending_thesis_cards_count=0,
            missed_stale_thesis_cards_count=0,
            dead_letter_count=0,
            recent_dead_letters=[],
            generated_at=generated_at,
        )
        try:
            article_counts = self._fetch_one(
                (
                    f"SELECT "
                    f"COUNT(DISTINCT article_id) AS articles_processed_count, "
                    f"COUNT(DISTINCT article_id) FILTER (WHERE is_market_moving = TRUE) "
                    f"AS market_moving_articles_count "
                    f"FROM {schema}.t_news_analyses "
                    f"WHERE analyzed_at >= %s AND analyzed_at < %s"
                ),
                (resolved_start_at, resolved_end_at),
            )
            card_counts = self._fetch_one(
                (
                    f"SELECT "
                    f"COUNT(*) FILTER (WHERE validation_status = 'valid') AS created_thesis_cards_count, "
                    f"COUNT(*) FILTER ("
                    f"WHERE validation_status = 'rejected' AND rejection_reason_code = 'stale_evidence'"
                    f") AS missed_stale_thesis_cards_count, "
                    f"AVG(evidence_age_exceeded_seconds) FILTER ("
                    f"WHERE validation_status = 'rejected' AND rejection_reason_code = 'stale_evidence'"
                    f") AS stale_evidence_exceeded_avg_seconds, "
                    f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY evidence_age_exceeded_seconds) FILTER ("
                    f"WHERE validation_status = 'rejected' AND rejection_reason_code = 'stale_evidence' "
                    f"AND evidence_age_exceeded_seconds IS NOT NULL"
                    f") AS stale_evidence_exceeded_p95_seconds, "
                    f"MAX(evidence_age_exceeded_seconds) FILTER ("
                    f"WHERE validation_status = 'rejected' AND rejection_reason_code = 'stale_evidence'"
                    f") AS stale_evidence_exceeded_max_seconds "
                    f"FROM {schema}.t_thesis_cards "
                    f"WHERE created_at >= %s AND created_at < %s"
                ),
                (resolved_start_at, resolved_end_at),
            )
            included_articles = self._scalar_count(
                (
                    f"SELECT COUNT(DISTINCT evidence_item->>'article_id') "
                    f"FROM {schema}.t_thesis_cards c "
                    f"CROSS JOIN LATERAL jsonb_array_elements(c.evidence) AS evidence_item "
                    f"WHERE c.created_at >= %s AND c.created_at < %s "
                    f"AND evidence_item ? 'article_id'"
                ),
                (resolved_start_at, resolved_end_at),
            )
            stale_articles = self._scalar_count(
                (
                    f"SELECT COUNT(DISTINCT article_id) FROM ("
                    f"SELECT article_id "
                    f"FROM {schema}.t_news_analyses "
                    f"WHERE analyzed_at >= %s AND analyzed_at < %s "
                    f"AND rejection_reason_code = 'stale_evidence' "
                    f"UNION "
                    f"SELECT evidence_item->>'article_id' AS article_id "
                    f"FROM {schema}.t_thesis_cards c "
                    f"CROSS JOIN LATERAL jsonb_array_elements(c.evidence) AS evidence_item "
                    f"WHERE c.created_at >= %s AND c.created_at < %s "
                    f"AND c.validation_status = 'rejected' "
                    f"AND c.rejection_reason_code = 'stale_evidence' "
                    f"AND evidence_item ? 'article_id'"
                    f") stale_articles "
                    f"WHERE article_id IS NOT NULL"
                ),
                (resolved_start_at, resolved_end_at, resolved_start_at, resolved_end_at),
            )
            pending_summary = self._fetch_one(
                (
                    f"SELECT COUNT(*) AS pending_thesis_cards_count, "
                    f"MAX(EXTRACT(EPOCH FROM (NOW() - window_started_at))) AS oldest_pending_age_seconds, "
                    f"AVG(EXTRACT(EPOCH FROM (NOW() - window_started_at))) AS average_pending_age_seconds, "
                    f"MIN(EXTRACT(EPOCH FROM ("
                    f"window_started_at + (%s * INTERVAL '1 minute') - NOW()"
                    f"))) AS minimum_pending_expires_in_seconds, "
                    f"AVG(EXTRACT(EPOCH FROM ("
                    f"window_started_at + (%s * INTERVAL '1 minute') - NOW()"
                    f"))) AS average_pending_expires_in_seconds "
                    f"FROM {schema}.t_evidence_windows "
                    f"WHERE status = 'collecting' "
                    f"AND window_started_at + (%s * INTERVAL '1 minute') > NOW()"
                ),
                (evidence_collection_max_minutes, evidence_collection_max_minutes, evidence_collection_max_minutes),
            )
            pending_windows = self._fetch_evidence_windows(
                evidence_collection_max_minutes=evidence_collection_max_minutes,
            )
            actionable_cards = self._fetch_actionable_thesis_cards(
                window_start_at=resolved_start_at,
                window_end_at=resolved_end_at,
            )
            dead_letter_count, recent_dead_letters = self._fetch_thesis_builder_dead_letters(
                recent_limit=_THESIS_BUILDER_RECENT_DEAD_LETTER_LIMIT,
            )
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            return empty_response

        return ThesisBuilderMetricsResponse(
            available=True,
            message=None,
            window=window,
            window_start_at=resolved_start_at,
            window_end_at=resolved_end_at,
            articles_processed_count=int(article_counts.get("articles_processed_count") or 0),
            market_moving_articles_count=int(article_counts.get("market_moving_articles_count") or 0),
            articles_included_in_cards_count=included_articles,
            stale_articles_count=stale_articles,
            created_thesis_cards_count=int(card_counts.get("created_thesis_cards_count") or 0),
            pending_thesis_cards_count=int(pending_summary.get("pending_thesis_cards_count") or 0),
            oldest_pending_age_seconds=_optional_float(pending_summary.get("oldest_pending_age_seconds")),
            average_pending_age_seconds=_optional_float(pending_summary.get("average_pending_age_seconds")),
            minimum_pending_expires_in_seconds=_optional_float(
                pending_summary.get("minimum_pending_expires_in_seconds")
            ),
            average_pending_expires_in_seconds=_optional_float(
                pending_summary.get("average_pending_expires_in_seconds")
            ),
            missed_stale_thesis_cards_count=int(card_counts.get("missed_stale_thesis_cards_count") or 0),
            stale_evidence_exceeded_avg_seconds=_optional_float(
                card_counts.get("stale_evidence_exceeded_avg_seconds")
            ),
            stale_evidence_exceeded_p95_seconds=_optional_float(
                card_counts.get("stale_evidence_exceeded_p95_seconds")
            ),
            stale_evidence_exceeded_max_seconds=_optional_float(
                card_counts.get("stale_evidence_exceeded_max_seconds")
            ),
            dead_letter_count=dead_letter_count,
            recent_dead_letters=recent_dead_letters,
            pending_windows=pending_windows,
            actionable_cards=actionable_cards,
            generated_at=generated_at,
        )

    def get_thesis_builder_consumer_health(
        self, *, consumer_group: str
    ) -> ThesisBuilderConsumerHealth:
        """Gather raw liveness signals for the ThesisBuilder queue consumer.

        Combines the freshness of ``t_news_analyses`` (work coming out) with the
        Redis consumer-group lag, pending count and per-consumer idle times (work
        going in). The ``stalled`` verdict is left to the service, which applies
        the configured threshold.
        """
        generated_at = _utc_now()

        last_analysis_age_seconds: float | None = None
        try:
            age_row = self._fetch_one(
                (
                    f"SELECT EXTRACT(EPOCH FROM (NOW() - MAX(analyzed_at))) AS age "
                    f"FROM {self._thesis_builder_schema}.t_news_analyses"
                ),
                (),
            )
            last_analysis_age_seconds = _optional_float(age_row.get("age"))
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            last_analysis_age_seconds = None

        stream = self._news_raw_queue
        try:
            stream_length = int(self._redis.xlen(stream))
            groups = self._redis.xinfo_groups(stream)
        except redis.RedisError as exc:
            return ThesisBuilderConsumerHealth(
                available=False,
                message=f"ThesisBuilder consumer telemetry unavailable: {exc}",
                consumer_group=consumer_group,
                last_analysis_age_seconds=last_analysis_age_seconds,
                generated_at=generated_at,
            )

        group = next((g for g in groups if g.get("name") == consumer_group), None)
        if group is None:
            return ThesisBuilderConsumerHealth(
                available=True,
                consumer_group=consumer_group,
                group_present=False,
                stream_length=stream_length,
                last_analysis_age_seconds=last_analysis_age_seconds,
                generated_at=generated_at,
            )

        try:
            consumers = self._redis.xinfo_consumers(stream, consumer_group)
        except redis.RedisError:
            consumers = []
        idle_seconds = [
            c["idle"] / 1000.0 for c in consumers if c.get("idle") is not None
        ]

        return ThesisBuilderConsumerHealth(
            available=True,
            consumer_group=consumer_group,
            group_present=True,
            stream_length=stream_length,
            consumer_lag=_optional_int(group.get("lag")),
            pending_count=_optional_int(group.get("pending")),
            consumer_count=len(consumers),
            min_consumer_idle_seconds=min(idle_seconds) if idle_seconds else None,
            oldest_consumer_idle_seconds=max(idle_seconds) if idle_seconds else None,
            last_analysis_age_seconds=last_analysis_age_seconds,
            generated_at=generated_at,
        )

    def get_window_articles(self, *, window_id: int) -> WindowArticlesResponse:
        schema = self._thesis_builder_schema
        generated_at = _utc_now()
        sql = (
            f"SELECT DISTINCT ON (article_id) "
            f"article_id, article_snapshot, confidence, is_market_moving, "
            f"validation_status, rejection_reason_code, analyzed_at "
            f"FROM {schema}.t_news_analyses "
            f"WHERE id IN ("
            f"SELECT jsonb_array_elements_text(analysis_ids)::bigint "
            f"FROM {schema}.t_evidence_windows WHERE id = %s"
            f") "
            f"ORDER BY article_id, analyzed_at DESC"
        )
        try:
            with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (window_id,))
                rows = cur.fetchall()
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            return WindowArticlesResponse(
                available=False,
                message="ThesisBuilder telemetry is unavailable.",
                window_id=window_id,
                articles=[],
                generated_at=generated_at,
            )

        articles = [_window_article(row) for row in rows]
        return WindowArticlesResponse(
            available=True,
            window_id=window_id,
            articles=articles,
            generated_at=generated_at,
        )

    def get_thesis_card_articles(self, *, card_id: str) -> WindowArticlesResponse:
        schema = self._thesis_builder_schema
        generated_at = _utc_now()
        sql = (
            f"SELECT DISTINCT ON (article_id) "
            f"article_id, article_snapshot, confidence, is_market_moving, "
            f"validation_status, rejection_reason_code, analyzed_at "
            f"FROM {schema}.t_news_analyses "
            f"WHERE id IN ("
            f"SELECT jsonb_array_elements_text(source_analysis_ids)::bigint "
            f"FROM {schema}.t_thesis_cards WHERE id = %s"
            f") "
            f"ORDER BY article_id, analyzed_at DESC"
        )
        try:
            with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (card_id,))
                rows = cur.fetchall()
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            return WindowArticlesResponse(
                available=False,
                message="ThesisBuilder telemetry is unavailable.",
                card_id=card_id,
                articles=[],
                generated_at=generated_at,
            )

        articles = [_window_article(row) for row in rows]
        return WindowArticlesResponse(
            available=True,
            card_id=card_id,
            articles=articles,
            generated_at=generated_at,
        )

    def get_news_analyses(
        self, *, window_start_at: datetime, window_end_at: datetime, limit: int
    ) -> NewsAnalysesResponse:
        schema = self._thesis_builder_schema
        generated_at = _utc_now()
        sql = (
            f"SELECT id, article_id, ticker, exchange_code, analyzed_at, "
            f"content_type, validation_status, rejection_reason_code, "
            f"confidence, is_market_moving, direction, strategy, reasoning, "
            f"article_snapshot->>'headline' AS headline, "
            f"article_snapshot->>'summary' AS summary, "
            f"article_snapshot->>'url' AS url, "
            f"article_snapshot->>'source' AS source "
            f"FROM {schema}.t_news_analyses "
            f"WHERE analyzed_at >= %s AND analyzed_at < %s "
            f"ORDER BY analyzed_at DESC, id DESC "
            f"LIMIT %s"
        )
        try:
            with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, (window_start_at, window_end_at, limit + 1))
                rows = cur.fetchall()
        except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn):
            return NewsAnalysesResponse(
                available=False,
                message="ThesisBuilder telemetry is unavailable.",
                generated_at=generated_at,
            )
        has_more = len(rows) > limit
        items = [
            NewsAnalysisItem(
                id=int(row["id"]),
                article_id=str(row["article_id"]),
                ticker=str(row["ticker"]),
                exchange_code=str(row["exchange_code"]),
                analyzed_at=_to_utc(row["analyzed_at"]),
                content_type=str(row["content_type"]) if row.get("content_type") else None,
                validation_status=str(row["validation_status"]),
                rejection_reason_code=str(row["rejection_reason_code"]) if row.get("rejection_reason_code") else None,
                confidence=float(row["confidence"]),
                is_market_moving=bool(row["is_market_moving"]),
                direction=str(row["direction"]) if row.get("direction") else None,
                strategy=str(row["strategy"]) if row.get("strategy") else None,
                reasoning=str(row["reasoning"]) if row.get("reasoning") else None,
                headline=str(row["headline"]) if row.get("headline") else None,
                summary=str(row["summary"]) if row.get("summary") else None,
                url=str(row["url"]) if row.get("url") else None,
                source=str(row["source"]) if row.get("source") else None,
            )
            for row in rows[:limit]
        ]
        return NewsAnalysesResponse(
            available=True,
            items=items,
            has_more=has_more,
            generated_at=generated_at,
        )

    def get_backlog(self) -> BacklogResponse:
        sql = (
            f"SELECT "
            f"COUNT(*) FILTER (WHERE status = 'pending') AS pending_count, "
            f"COUNT(*) FILTER (WHERE status = 'publishing') AS retrying_count, "
            f"COUNT(*) FILTER (WHERE status = 'dead_lettered') AS dead_letter_count, "
            f"EXTRACT(EPOCH FROM ("
            f"NOW() - MIN(CASE WHEN status IN ('pending', 'publishing') THEN created_at END)"
            f")) AS max_attempt_age_seconds "
            f"FROM {self._news_schema}.t_publication_obligations"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            row = cur.fetchone() or {}

        return BacklogResponse(
            pending_count=int(row.get("pending_count") or 0),
            retrying_count=int(row.get("retrying_count") or 0),
            dead_letter_count=int(row.get("dead_letter_count") or 0),
            max_attempt_age_seconds=_optional_float(row.get("max_attempt_age_seconds")),
            generated_at=_utc_now(),
        )

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        sql = (
            f"SELECT obligation_id, source_key, canonical_event_id, last_error_code, created_at, updated_at "
            f"FROM {self._news_schema}.t_publication_obligations "
            f"WHERE status = 'dead_lettered' "
            f"ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (limit, offset))
            rows = cur.fetchall()

        items = [
            DeadLetterItem(
                obligation_id=row["obligation_id"],
                source_key=row["source_key"],
                canonical_event_id=row["canonical_event_id"],
                reason=row["last_error_code"],
                first_failure_at=_to_utc(row["created_at"]),
                updated_at=_to_utc(row["updated_at"]),
            )
            for row in rows
        ]
        return DeadLetterResponse(items=items, limit=limit, offset=offset, generated_at=_utc_now())

    def get_filter_quality_status(self) -> FilterQualityStatusResponse:
        return FilterQualityStatusResponse(
            running_run=self.get_running_filter_quality_run(),
            last_run=self.get_last_filter_quality_run(),
            generated_at=_utc_now(),
        )

    def list_filter_quality_incorrectly_rejected(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyRejectedResponse:
        sql = (
            f"SELECT a.assessment_id, a.run_id, a.article_id, i.headline, i.summary, i.url, a.source, a.published_at, "
            f"pfr.matched_article_id AS production_matched_article_id, "
            f"matched_pfr_i.headline AS production_matched_article_headline, "
            f"matched_pfr_i.url AS production_matched_article_url, "
            f"matched_pfr_i.source AS production_matched_article_source, "
            f"matched_pfr_i.published_at AS production_matched_article_published_at, "
            f"sfr_result.matched_article_id AS simulation_matched_article_id, "
            f"matched_sfr_i.headline AS simulation_matched_article_headline, "
            f"matched_sfr_i.url AS simulation_matched_article_url, "
            f"matched_sfr_i.source AS simulation_matched_article_source, "
            f"matched_sfr_i.published_at AS simulation_matched_article_published_at, "
            f"a.production_filter_outcome, a.simulation_filter_outcome, a.rejection_reason_code, "
            f"pfr.rejection_reason_code AS production_rejection_reason_code, "
            f"sfr_result.rejection_reason_code AS simulation_rejection_reason_code, "
            f"a.probable_cause, a.improvement_suggestion, a.rationale, a.classification_confidence, "
            f"a.suggestion_json, sfr.filter_config_snapshot_json, a.evaluated_at "
            f"FROM {self._filter_quality_schema}.t_filter_quality_item_assessments a "
            f"JOIN {self._news_schema}.t_input_news_articles i ON i.id = a.article_id "
            f"LEFT JOIN {self._news_schema}.t_news_filter_runs sfr ON sfr.filter_run_id = a.filter_run_id_simulation "
            f"LEFT JOIN {self._news_schema}.t_news_filter_results pfr "
            f"ON pfr.filter_run_id = a.filter_run_id_production AND pfr.article_id = a.article_id "
            f"LEFT JOIN {self._news_schema}.t_news_filter_results sfr_result "
            f"ON sfr_result.filter_run_id = a.filter_run_id_simulation AND sfr_result.article_id = a.article_id "
            f"LEFT JOIN {self._news_schema}.t_input_news_articles matched_pfr_i "
            f"ON matched_pfr_i.id = pfr.matched_article_id "
            f"LEFT JOIN {self._news_schema}.t_input_news_articles matched_sfr_i "
            f"ON matched_sfr_i.id = sfr_result.matched_article_id "
            f"WHERE a.run_id = %s "
            f"AND a.item_status = 'evaluated' "
            f"AND a.classification_label = 'incorrectly_rejected' "
            f"ORDER BY a.evaluated_at DESC, a.article_id"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, (run_id,))
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return FilterQualityIncorrectlyRejectedResponse(run_id=run_id, items=[], generated_at=_utc_now())
            rows = cur.fetchall()
        return FilterQualityIncorrectlyRejectedResponse(
            run_id=run_id,
            items=[_incorrectly_rejected_item(row) for row in rows],
            generated_at=_utc_now(),
        )

    def list_filter_quality_incorrectly_accepted(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyAcceptedResponse:
        sql = (
            f"SELECT a.assessment_id, a.run_id, a.article_id, i.headline, i.summary, i.url, a.source, a.published_at, "
            f"a.production_filter_outcome, a.simulation_filter_outcome, a.probable_cause, "
            f"a.improvement_suggestion, a.rationale, a.classification_confidence, a.suggestion_json, a.evaluated_at "
            f"FROM {self._filter_quality_schema}.t_filter_quality_item_assessments a "
            f"JOIN {self._news_schema}.t_input_news_articles i ON i.id = a.article_id "
            f"WHERE a.run_id = %s "
            f"AND a.item_status = 'evaluated' "
            f"AND a.evaluation_scope = 'accepted_audit' "
            f"AND a.classification_label = 'incorrectly_accepted' "
            f"ORDER BY a.evaluated_at DESC, a.article_id"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, (run_id,))
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return FilterQualityIncorrectlyAcceptedResponse(run_id=run_id, items=[], generated_at=_utc_now())
            rows = cur.fetchall()
        return FilterQualityIncorrectlyAcceptedResponse(
            run_id=run_id,
            items=[_incorrectly_accepted_item(row) for row in rows],
            generated_at=_utc_now(),
        )

    def list_fetched_articles(
        self,
        *,
        fetched_since: datetime,
        limit: int,
    ) -> FetchedArticlesResponse:
        sql = (
            f"SELECT article_id, source, source_key, headline, summary, url, tickers, "
            f"published_at, fetched_at, filter_outcome, rejection_reason_code, matched_article_id "
            f"FROM ("
            f"SELECT DISTINCT ON (a.id) a.id AS article_id, a.source, a.source_key, a.headline, "
            f"a.summary, a.url, a.tickers, a.published_at, a.fetched_at, "
            f"r.filter_outcome, r.rejection_reason_code, r.matched_article_id "
            f"FROM {self._news_schema}.t_input_news_articles a "
            f"LEFT JOIN {self._news_schema}.t_news_filter_runs fr ON fr.run_mode = 'production' "
            f"LEFT JOIN {self._news_schema}.t_news_filter_results r "
            f"ON r.article_id = a.id AND r.filter_run_id = fr.filter_run_id "
            f"WHERE a.fetched_at >= %s "
            f"ORDER BY a.id, r.created_at DESC NULLS LAST"
            f") latest "
            f"ORDER BY fetched_at DESC "
            f"LIMIT %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, (fetched_since, limit))
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return FetchedArticlesResponse(items=[], generated_at=_utc_now())
            rows = cur.fetchall()
        return FetchedArticlesResponse(
            items=[_fetched_article(row) for row in rows],
            generated_at=_utc_now(),
        )

    def get_production_filter_config(self) -> NewsFilterConfigPayload:
        config = self._load_filter_config(role="production")
        if config is None:
            config = self._seed_production_filter_from_env()
        return _filter_config_payload(config)

    def get_test_filter_config(self) -> NewsFilterConfigPayload:
        config = self._load_filter_config(role="test")
        if config is not None:
            return _filter_config_payload(config)
        production = self.get_production_filter_config()
        return self.save_test_filter_config(
            NewsFilterConfigPayload(
                config_name="Test filter",
                include_keywords=production.include_keywords,
                exclude_keywords=production.exclude_keywords,
                watchlist_tickers=production.watchlist_tickers,
                dedupe_algorithm=production.dedupe_algorithm,
                dedupe_similarity_threshold=production.dedupe_similarity_threshold,
                dedupe_lookback_hours=production.dedupe_lookback_hours,
            )
        )

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        config = config_from_snapshot(
            _payload_snapshot(payload),
            filter_config_id=payload.filter_config_id or new_filter_config_id(),
            config_name=payload.config_name or "Test filter",
            config_role="test",
            status="active",
            created_from_run_id=payload.created_from_run_id,
        )
        self._upsert_filter_config(config)
        return _filter_config_payload(config)

    def promote_test_filter_config(self) -> NewsFilterConfigPayload:
        test = self._load_filter_config(role="test")
        if test is None:
            raise ValueError("missing_test_filter_config")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self._news_schema}.t_news_filter_configs "
                f"SET status = 'archived', updated_at = NOW() "
                f"WHERE config_role = 'production' AND status = 'active'"
            )
            production = config_from_snapshot(
                test.snapshot(),
                filter_config_id=new_filter_config_id(),
                config_name="Production filter",
                config_role="production",
                status="active",
                created_from_run_id=test.created_from_run_id,
            )
            cur.execute(
                f"INSERT INTO {self._news_schema}.t_news_filter_configs "
                f"(filter_config_id, config_name, config_role, status, include_keywords, exclude_keywords, "
                f"watchlist_tickers, dedupe_algorithm, dedupe_similarity_threshold, dedupe_lookback_hours, "
                f"created_from_run_id, activated_at) "
                f"VALUES (%s, %s, 'production', 'active', %s, %s, %s, %s, %s, %s, %s, NOW())",
                (
                    production.filter_config_id,
                    production.config_name,
                    Json(list(production.include_keywords)),
                    Json(list(production.exclude_keywords)),
                    Json(list(production.watchlist_tickers)),
                    production.dedupe_algorithm,
                    production.dedupe_similarity_threshold,
                    production.dedupe_lookback_hours,
                    production.created_from_run_id,
                ),
            )
        return _filter_config_payload(production)

    def get_running_filter_quality_run(self) -> FilterQualityRunSummary | None:
        sql = (
            f"SELECT {_filter_quality_run_columns(self._filter_quality_schema, self._news_schema)} "
            f"FROM {self._filter_quality_schema}.t_filter_quality_runs r "
            f"WHERE r.status = 'running' "
            f"ORDER BY r.created_at DESC LIMIT 1"
        )
        return self._fetch_filter_quality_run(sql, ())

    def get_last_filter_quality_run(self) -> FilterQualityRunSummary | None:
        sql = (
            f"SELECT {_filter_quality_run_columns(self._filter_quality_schema, self._news_schema)} "
            f"FROM {self._filter_quality_schema}.t_filter_quality_runs r "
            f"WHERE r.status IN ('completed', 'failed') "
            f"ORDER BY r.finished_at DESC NULLS LAST, r.created_at DESC LIMIT 1"
        )
        return self._fetch_filter_quality_run(sql, ())

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int:
        cutoff = _utc_now() - timedelta(seconds=timeout_seconds)
        sql = (
            f"UPDATE {self._filter_quality_schema}.t_filter_quality_runs "
            f"SET status = 'failed', finished_at = NOW(), error_code = 'stale_running_run', "
            f"error_details_json = '{{}}'::jsonb "
            f"WHERE status = 'running' AND started_at < %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(sql, (cutoff,))
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return 0
            return int(cur.rowcount or 0)

    def list_backtest_runs(self, *, window_start_at: datetime) -> list["BacktestRunRow"]:
        sql = (
            f"SELECT {_BACKTEST_RUN_COLUMNS} "
            f"FROM {self._backtester_schema}.t_backtest_runs "
            f"WHERE created_at >= %s "
            f"ORDER BY created_at DESC"
        )
        rows = self._fetch_backtest_rows(sql, (_to_utc(window_start_at),))
        return [_backtest_run_row(row) for row in rows]

    def get_active_backtest_run(self) -> "BacktestRunRow | None":
        sql = (
            f"SELECT {_BACKTEST_RUN_COLUMNS} "
            f"FROM {self._backtester_schema}.t_backtest_runs "
            f"WHERE status = 'running' "
            f"ORDER BY created_at DESC LIMIT 1"
        )
        rows = self._fetch_backtest_rows(sql, ())
        return _backtest_run_row(rows[0]) if rows else None

    def get_backtest_run(self, *, run_id: str) -> "BacktestRunRow | None":
        sql = (
            f"SELECT {_BACKTEST_RUN_COLUMNS} "
            f"FROM {self._backtester_schema}.t_backtest_runs "
            f"WHERE run_id = %s LIMIT 1"
        )
        rows = self._fetch_backtest_rows(sql, (run_id,))
        return _backtest_run_row(rows[0]) if rows else None

    def list_backtest_trades(
        self,
        *,
        run_id: str,
        timing_scenario: str | None = None,
        strategy: str | None = None,
        exit_reason: str | None = None,
        card_status: str | None = None,
        limit: int,
        offset: int,
    ) -> list["BacktestTradeRow"]:
        where, params = self._backtest_trade_filters(
            run_id=run_id,
            timing_scenario=timing_scenario,
            strategy=strategy,
            exit_reason=exit_reason,
            card_status=card_status,
        )
        sql = (
            f"SELECT trade_id, ticker, exchange_code, strategy, direction, entry_timing_scenario, "
            f"entry_at, entry_price, exit_at, exit_price, net_pnl, return_pct, exit_reason, "
            f"risk_block_rule, news_fetch_delay_seconds, thesis_build_delay_seconds, "
            f"total_pipeline_delay_seconds, card_decision_state, card_was_live_expired "
            f"FROM {self._backtester_schema}.t_backtest_trades "
            f"WHERE {where} "
            f"ORDER BY entry_at ASC NULLS LAST, trade_id "
            f"LIMIT %s OFFSET %s"
        )
        rows = self._fetch_backtest_rows(sql, (*params, limit, offset))
        return [_backtest_trade_row(row) for row in rows]

    def count_backtest_trades(
        self,
        *,
        run_id: str,
        timing_scenario: str | None = None,
        strategy: str | None = None,
        exit_reason: str | None = None,
        card_status: str | None = None,
    ) -> int:
        where, params = self._backtest_trade_filters(
            run_id=run_id,
            timing_scenario=timing_scenario,
            strategy=strategy,
            exit_reason=exit_reason,
            card_status=card_status,
        )
        sql = (
            f"SELECT COUNT(*) AS total "
            f"FROM {self._backtester_schema}.t_backtest_trades "
            f"WHERE {where}"
        )
        rows = self._fetch_backtest_rows(sql, tuple(params))
        return int(rows[0]["total"]) if rows else 0

    def list_backtest_equity_points(self, *, run_id: str) -> list["BacktestEquityRow"]:
        sql = (
            f"SELECT timing_scenario, as_of, equity, open_positions "
            f"FROM {self._backtester_schema}.t_backtest_equity_points "
            f"WHERE run_id = %s "
            f"ORDER BY timing_scenario, as_of"
        )
        rows = self._fetch_backtest_rows(sql, (run_id,))
        return [_backtest_equity_row(row) for row in rows]

    def list_backtest_cards(self, *, run_id: str) -> list["BacktestCardRow"]:
        sql = (
            f"SELECT cs.thesis_card_id, cs.ticker, cs.exchange_code, cs.direction, cs.strategy, "
            f"cs.time_horizon, cs.confidence, cs.decision_state, cs.card_created_at, cs.card_expires_at, "
            f"t.trade_id, t.entry_timing_scenario, t.entry_at, t.entry_price, "
            f"t.exit_at, t.exit_price, t.net_pnl, t.return_pct, t.exit_reason, t.risk_block_rule "
            f"FROM {self._backtester_schema}.t_backtest_card_snapshots cs "
            f"LEFT JOIN {self._backtester_schema}.t_backtest_trades t "
            f"ON t.run_id = cs.run_id AND t.thesis_card_id = cs.thesis_card_id "
            f"WHERE cs.run_id = %s "
            f"ORDER BY cs.card_created_at, cs.thesis_card_id, t.entry_timing_scenario"
        )
        rows = self._fetch_backtest_rows(sql, (run_id,))
        return _group_backtest_cards(rows)

    def _backtest_trade_filters(
        self,
        *,
        run_id: str,
        timing_scenario: str | None,
        strategy: str | None,
        exit_reason: str | None,
        card_status: str | None,
    ) -> tuple[str, list[Any]]:
        clauses = ["run_id = %s"]
        params: list[Any] = [run_id]
        if timing_scenario:
            clauses.append("entry_timing_scenario = %s")
            params.append(timing_scenario)
        if strategy:
            clauses.append("strategy = %s")
            params.append(strategy)
        if exit_reason:
            clauses.append("exit_reason = %s")
            params.append(exit_reason)
        if card_status:
            clauses.append("card_decision_state = %s")
            params.append(card_status)
        return " AND ".join(clauses), params

    def _fetch_backtest_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, params)
            except (errors.InvalidSchemaName, errors.UndefinedTable, errors.UndefinedColumn) as exc:
                raise BacktesterTablesUnavailable(str(exc)) from exc
            return [dict(row) for row in cur.fetchall()]

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=True, connect_timeout=self._query_timeout_seconds)

    def _count_articles(self, source_key: str) -> int:
        sql = f"SELECT COUNT(*) FROM {self._news_schema}.t_news_articles WHERE source = %s"
        return self._scalar_count(sql, (source_key,))

    def _count_published(self, source_key: str) -> int:
        sql = (
            f"SELECT COUNT(*) FROM {self._news_schema}.t_publication_obligations "
            f"WHERE source_key = %s AND status = 'published'"
        )
        return self._scalar_count(sql, (source_key,))

    def _scalar_count(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return dict(row or {})

    def _fetch_evidence_windows(
        self,
        *,
        evidence_collection_max_minutes: int,
    ) -> list[ThesisBuilderEvidenceWindow]:
        sql = (
            f"SELECT id, ticker, exchange_code, strategy, direction, window_started_at, last_evidence_at, "
            f"EXTRACT(EPOCH FROM (NOW() - window_started_at)) AS pending_age_seconds, "
            f"EXTRACT(EPOCH FROM (window_started_at + (%s * INTERVAL '1 minute') - NOW())) "
            f"AS expires_in_seconds, "
            f"jsonb_array_length(article_ids) AS evidence_count, "
            f"required_evidence_count "
            f"FROM {self._thesis_builder_schema}.t_evidence_windows "
            f"WHERE status = 'collecting' "
            f"AND window_started_at + (%s * INTERVAL '1 minute') > NOW() "
            f"ORDER BY window_started_at ASC LIMIT 25"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (evidence_collection_max_minutes, evidence_collection_max_minutes))
            rows = cur.fetchall()
        return [
            ThesisBuilderEvidenceWindow(
                window_id=int(row["id"]),
                ticker=str(row["ticker"]),
                exchange_code=str(row["exchange_code"]),
                strategy=str(row["strategy"]),
                direction=str(row["direction"]) if row["direction"] else None,
                window_started_at=_to_utc(row["window_started_at"]),
                last_evidence_at=_to_utc(row["last_evidence_at"]),
                pending_age_seconds=float(row["pending_age_seconds"] or 0),
                expires_in_seconds=float(row["expires_in_seconds"] or 0),
                evidence_count=int(row["evidence_count"] or 0),
                required_evidence_count=int(row["required_evidence_count"]),
            )
            for row in rows
        ]

    def _fetch_actionable_thesis_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
    ) -> list[ThesisBuilderActionableCard]:
        sql = (
            f"SELECT id, ticker, exchange_code, strategy, direction, time_horizon, confidence, "
            f"created_at, expires_at, "
            f"EXTRACT(EPOCH FROM (expires_at - NOW())) AS expires_in_seconds, "
            f"jsonb_array_length(evidence) AS evidence_count, "
            f"(signal_published_at IS NOT NULL) AS signal_published "
            f"FROM {self._thesis_builder_schema}.t_thesis_cards "
            f"WHERE validation_status = 'valid' "
            f"AND direction IN ('buy', 'sell') "
            f"AND created_at >= %s AND created_at < %s "
            f"ORDER BY created_at DESC LIMIT 25"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (window_start_at, window_end_at))
            rows = cur.fetchall()
        return [
            ThesisBuilderActionableCard(
                card_id=str(row["id"]),
                ticker=str(row["ticker"]),
                exchange_code=str(row["exchange_code"]),
                strategy=str(row["strategy"]),
                direction=str(row["direction"]),
                time_horizon=str(row["time_horizon"]),
                confidence=float(row["confidence"] or 0),
                created_at=_to_utc(row["created_at"]),
                expires_at=_to_utc(row["expires_at"]),
                expires_in_seconds=float(row["expires_in_seconds"] or 0),
                evidence_count=int(row["evidence_count"] or 0),
                signal_published=bool(row["signal_published"]),
            )
            for row in rows
        ]

    def fetch_thesis_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        limit: int = 500,
    ) -> list[ThesisCardSummary]:
        sql = (
            f"SELECT id, ticker, exchange_code, strategy, direction, time_horizon, confidence, "
            f"created_at, expires_at, "
            f"EXTRACT(EPOCH FROM (expires_at - NOW())) AS expires_in_seconds, "
            f"jsonb_array_length(evidence) AS evidence_count, "
            f"(signal_published_at IS NOT NULL) AS signal_published, "
            f"validation_status, rejection_reason_code "
            f"FROM {self._thesis_builder_schema}.t_thesis_cards "
            f"WHERE created_at >= %s AND created_at < %s "
            f"ORDER BY created_at DESC LIMIT %s"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (window_start_at, window_end_at, limit))
            rows = cur.fetchall()
        return [
            ThesisCardSummary(
                card_id=str(row["id"]),
                ticker=str(row["ticker"]),
                exchange_code=str(row["exchange_code"]),
                strategy=str(row["strategy"]),
                direction=str(row["direction"]),
                time_horizon=str(row["time_horizon"]),
                confidence=float(row["confidence"] or 0),
                created_at=_to_utc(row["created_at"]),
                expires_at=_to_utc(row["expires_at"]),
                expires_in_seconds=float(row["expires_in_seconds"] or 0),
                evidence_count=int(row["evidence_count"] or 0),
                signal_published=bool(row["signal_published"]),
                validation_status=str(row["validation_status"]),
                rejection_reason_code=(
                    str(row["rejection_reason_code"])
                    if row["rejection_reason_code"]
                    else None
                ),
            )
            for row in rows
        ]

    def _fetch_thesis_builder_dead_letters(
        self,
        *,
        recent_limit: int,
    ) -> tuple[int, list[ThesisBuilderDeadLetterItem]]:
        current_max = "+"
        dead_letter_count = 0
        recent_items: list[ThesisBuilderDeadLetterItem] = []
        while True:
            rows = self._redis.xrevrange(
                self._failed_messages_dlq,
                max=current_max,
                min="-",
                count=_DLQ_SCAN_BATCH_SIZE,
            )
            if not rows:
                break
            for message_id, fields in rows:
                if fields.get("source_stream") != self._news_raw_queue:
                    continue
                dead_letter_count += 1
                if len(recent_items) >= recent_limit:
                    continue
                payload = _json_object(fields.get("payload_json"))
                article_id = str(payload.get("id") or fields.get("dedupe_key") or "").strip()
                if not article_id:
                    article_id = str(fields.get("source_message_id") or message_id)
                recent_items.append(
                    ThesisBuilderDeadLetterItem(
                        source_message_id=str(fields.get("source_message_id") or message_id),
                        article_id=article_id,
                        error_code=str(fields.get("error_code")).strip() if fields.get("error_code") else None,
                        failed_at=_stream_message_id_to_utc(message_id),
                    )
                )
            current_max = f"({rows[-1][0]}"
        return dead_letter_count, recent_items

    def _fetch_filter_quality_run(
        self,
        sql: str,
        params: tuple[Any, ...],
    ) -> FilterQualityRunSummary | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, params)
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return None
            row = cur.fetchone()
        return _filter_quality_run(row) if row else None

    def _load_filter_config(self, *, role: str) -> NewsFilterConfig | None:
        sql = (
            f"SELECT filter_config_id, config_name, config_role, status, include_keywords, exclude_keywords, "
            f"watchlist_tickers, dedupe_algorithm, dedupe_similarity_threshold, dedupe_lookback_hours, "
            f"created_from_run_id "
            f"FROM {self._news_schema}.t_news_filter_configs "
            f"WHERE config_role = %s AND status = 'active' "
            f"ORDER BY activated_at DESC NULLS LAST, updated_at DESC LIMIT 1"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(sql, (role,))
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return None
            row = cur.fetchone()
        return _news_filter_config(row) if row else None

    def _seed_production_filter_from_env(self) -> NewsFilterConfig:
        settings = NewsFetcherSettings.from_env()
        config = NewsFilterConfig(
            filter_config_id=new_filter_config_id(),
            config_name="Production filter",
            config_role="production",
            status="active",
            include_keywords=normalize_keywords(settings.include_keywords),
            exclude_keywords=normalize_keywords(settings.exclude_keywords),
            watchlist_tickers=normalize_tickers(self._load_active_watchlist_tickers(settings)),
            dedupe_algorithm=settings.dedupe_algorithm,
            dedupe_similarity_threshold=settings.dedupe_similarity_threshold,
            dedupe_lookback_hours=settings.dedupe_lookback_hours,
        )
        self._upsert_filter_config(config)
        return config

    def _upsert_filter_config(self, config: NewsFilterConfig) -> None:
        sql = (
            f"INSERT INTO {self._news_schema}.t_news_filter_configs "
            f"(filter_config_id, config_name, config_role, status, include_keywords, exclude_keywords, "
            f"watchlist_tickers, dedupe_algorithm, dedupe_similarity_threshold, dedupe_lookback_hours, "
            f"created_from_run_id, activated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'active' THEN NOW() ELSE NULL END) "
            f"ON CONFLICT (filter_config_id) DO UPDATE SET "
            f"config_name = EXCLUDED.config_name, status = EXCLUDED.status, "
            f"include_keywords = EXCLUDED.include_keywords, exclude_keywords = EXCLUDED.exclude_keywords, "
            f"watchlist_tickers = EXCLUDED.watchlist_tickers, dedupe_algorithm = EXCLUDED.dedupe_algorithm, "
            f"dedupe_similarity_threshold = EXCLUDED.dedupe_similarity_threshold, "
            f"dedupe_lookback_hours = EXCLUDED.dedupe_lookback_hours, "
            f"created_from_run_id = EXCLUDED.created_from_run_id, updated_at = NOW()"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    config.filter_config_id,
                    config.config_name,
                    config.config_role,
                    config.status,
                    Json(list(config.include_keywords)),
                    Json(list(config.exclude_keywords)),
                    Json(list(config.watchlist_tickers)),
                    config.dedupe_algorithm,
                    config.dedupe_similarity_threshold,
                    config.dedupe_lookback_hours,
                    config.created_from_run_id,
                    config.status,
                ),
            )

    def _load_active_watchlist_tickers(self, settings: NewsFetcherSettings) -> set[str]:
        safe_shared = _safe_identifier(settings.shared_db_schema)
        safe_table = _safe_identifier(settings.watchlist_table)
        sql = f"SELECT ticker FROM {safe_shared}.{safe_table} WHERE is_active = TRUE"
        with self._connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(sql)
            except (errors.InvalidSchemaName, errors.UndefinedTable):
                return set()
            return {str(row[0]).strip().upper() for row in cur.fetchall() if str(row[0]).strip()}


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _window_to_timedelta(window: str) -> timedelta:
    normalized = window.strip().lower()
    if normalized == "24h":
        normalized = "1d"
    allowed = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }
    try:
        return allowed[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported throughput window: {normalized}") from exc


def _throughput_granularity_for_window(window: str) -> str:
    normalized = window.strip().lower()
    if normalized == "24h":
        normalized = "1d"
    if normalized in {"15m", "1h"}:
        return "raw"
    if normalized in {"1d", "7d"}:
        return "hour"
    if normalized == "30d":
        return "day"
    raise ValueError(f"Unsupported throughput window: {normalized}")


def _throughput_bucket_expression(window: str) -> str:
    granularity = _throughput_granularity_for_window(window)
    if granularity == "raw":
        return "created_at"
    if granularity == "hour":
        return "date_trunc('hour', created_at)"
    return "date_trunc('day', created_at)"


def _analysis_throughput_bucket_expression(window: str) -> str:
    granularity = _throughput_granularity_for_window(window)
    if granularity == "raw":
        return "analyzed_at"
    if granularity == "hour":
        return "date_trunc('hour', analyzed_at)"
    return "date_trunc('day', analyzed_at)"


def _message_event_bucket_expression(window: str) -> str:
    granularity = _throughput_granularity_for_window(window)
    if granularity == "raw":
        return "processed_at"
    if granularity == "hour":
        return "date_trunc('hour', processed_at)"
    return "date_trunc('day', processed_at)"


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _to_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _window_article(row: dict[str, Any]) -> WindowArticle:
    snapshot = _json_object(row.get("article_snapshot"))
    return WindowArticle(
        article_id=str(row["article_id"]),
        headline=str(snapshot.get("headline") or ""),
        url=str(snapshot.get("url") or ""),
        source=str(snapshot.get("source") or row.get("source") or ""),
        summary=snapshot.get("summary") or None,
        published_at=_parse_optional_datetime(snapshot.get("published_at")),
        confidence=_optional_float(row.get("confidence")),
        is_market_moving=bool(row.get("is_market_moving")),
        validation_status=row.get("validation_status"),
        rejection_reason_code=row.get("rejection_reason_code"),
    )


def _stream_message_id_to_utc(message_id: str) -> datetime:
    millis_text = message_id.split("-", 1)[0]
    try:
        millis = int(millis_text)
    except ValueError:
        return _utc_now()
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _filter_quality_run_columns(schema: str, news_schema: str) -> str:
    return (
        "r.run_id, r.status, r.news_window_start_at, r.news_window_end_at, r.created_at, r.started_at, "
        "r.finished_at, r.error_code, r.rejection_precision_proxy, r.incorrectly_accepted_rate_estimate, "
        "r.dataset_input_count, r.dataset_rejected_count, r.dataset_accepted_count, r.rejected_items_evaluated, "
        "r.accepted_items_sampled, r.correctly_rejected_count, r.incorrectly_rejected_count, "
        "r.correctly_accepted_count, r.incorrectly_accepted_count, "
        f"(SELECT COUNT(1) FROM {schema}.t_filter_quality_item_assessments a "
        "WHERE a.run_id = r.run_id AND a.item_status = 'failed') AS item_failed_count, "
        "(SELECT COALESCE(jsonb_object_agg(error_code, error_count), '{}'::jsonb) "
        "FROM (SELECT COALESCE(a.item_error_code, 'unknown') AS error_code, COUNT(1) AS error_count "
        f"FROM {schema}.t_filter_quality_item_assessments a "
        "WHERE a.run_id = r.run_id AND a.item_status = 'failed' GROUP BY 1) errors) AS item_error_codes, "
        f"(SELECT fr.filter_config_snapshot_json FROM {news_schema}.t_news_filter_runs fr "
        "WHERE fr.filter_run_id = CONCAT('sim_', r.run_id) LIMIT 1) AS evaluated_filter_config_snapshot_json, "
        "r.summary_json, r.recommendation_summary_md"
    )


def _filter_quality_run(row: dict[str, Any]) -> FilterQualityRunSummary:
    summary_json = dict(row["summary_json"] or {})
    dataset_input_count = int(row["dataset_input_count"])
    dataset_accepted_count = int(row["dataset_accepted_count"])
    accepted_items_sampled = int(row["accepted_items_sampled"])
    correctly_rejected_count = int(row["correctly_rejected_count"])
    correctly_accepted_count = int(row["correctly_accepted_count"])
    assumed_correct_accepted_count = int(
        summary_json.get("assumed_correct_accepted_count")
        if summary_json.get("assumed_correct_accepted_count") is not None
        else max(0, dataset_accepted_count - accepted_items_sampled)
    )
    total_correct_count = int(
        summary_json.get("total_correct_count")
        if summary_json.get("total_correct_count") is not None
        else correctly_rejected_count + correctly_accepted_count + assumed_correct_accepted_count
    )
    total_filter_quality = summary_json.get("total_filter_quality")
    if total_filter_quality is None and dataset_input_count > 0:
        total_filter_quality = total_correct_count / dataset_input_count
    return FilterQualityRunSummary(
        run_id=str(row["run_id"]),
        status=row["status"],
        news_window_start_at=_to_utc(row["news_window_start_at"]),
        news_window_end_at=_to_utc(row["news_window_end_at"]),
        created_at=_to_utc(row["created_at"]),
        started_at=_to_utc(row["started_at"]),
        finished_at=_to_utc(row["finished_at"]) if row["finished_at"] else None,
        error_code=row["error_code"],
        rejection_precision_proxy=_optional_float(row["rejection_precision_proxy"]),
        incorrectly_accepted_rate_estimate=_optional_float(row["incorrectly_accepted_rate_estimate"]),
        dataset_input_count=dataset_input_count,
        dataset_rejected_count=int(row["dataset_rejected_count"]),
        dataset_accepted_count=dataset_accepted_count,
        rejected_items_evaluated=int(row["rejected_items_evaluated"]),
        accepted_items_sampled=accepted_items_sampled,
        correctly_rejected_count=correctly_rejected_count,
        incorrectly_rejected_count=int(row["incorrectly_rejected_count"]),
        correctly_accepted_count=correctly_accepted_count,
        incorrectly_accepted_count=int(row["incorrectly_accepted_count"]),
        item_failed_count=int(row["item_failed_count"] or 0),
        item_error_codes={str(key): int(value) for key, value in dict(row["item_error_codes"] or {}).items()},
        total_filter_quality=_optional_float(total_filter_quality),
        total_correct_count=total_correct_count,
        assumed_correct_accepted_count=assumed_correct_accepted_count,
        evaluation_subject=str(summary_json.get("evaluation_subject") or "unknown"),
        evaluated_filter_config=_filter_config_payload_from_snapshot(
            row["evaluated_filter_config_snapshot_json"],
            created_from_run_id=str(row["run_id"]),
        ),
        summary_json=summary_json,
        recommendation_summary_md=str(row["recommendation_summary_md"] or ""),
    )


def _incorrectly_rejected_item(row: dict[str, Any]) -> FilterQualityIncorrectlyRejectedItem:
    filter_config_snapshot_json = dict(row["filter_config_snapshot_json"] or {})
    suggestion_json = sanitize_suggestion_json(
        dict(row["suggestion_json"] or {}),
        headline=str(row["headline"]),
        summary=row["summary"],
        existing_include_keywords=_string_list(filter_config_snapshot_json.get("include_keywords")),
    )
    return FilterQualityIncorrectlyRejectedItem(
        assessment_id=str(row["assessment_id"]),
        run_id=str(row["run_id"]),
        article_id=str(row["article_id"]),
        headline=str(row["headline"]),
        summary=row["summary"],
        url=str(row["url"]),
        source=str(row["source"]),
        published_at=_to_utc(row["published_at"]),
        production_matched_article_id=(
            str(row["production_matched_article_id"])
            if row.get("production_matched_article_id")
            else None
        ),
        production_matched_article_headline=(
            str(row["production_matched_article_headline"])
            if row.get("production_matched_article_headline")
            else None
        ),
        production_matched_article_url=(
            str(row["production_matched_article_url"])
            if row.get("production_matched_article_url")
            else None
        ),
        production_matched_article_source=(
            str(row["production_matched_article_source"])
            if row.get("production_matched_article_source")
            else None
        ),
        production_matched_article_published_at=(
            _to_utc(row["production_matched_article_published_at"])
            if row.get("production_matched_article_published_at")
            else None
        ),
        simulation_matched_article_id=(
            str(row["simulation_matched_article_id"])
            if row.get("simulation_matched_article_id")
            else None
        ),
        simulation_matched_article_headline=(
            str(row["simulation_matched_article_headline"])
            if row.get("simulation_matched_article_headline")
            else None
        ),
        simulation_matched_article_url=(
            str(row["simulation_matched_article_url"])
            if row.get("simulation_matched_article_url")
            else None
        ),
        simulation_matched_article_source=(
            str(row["simulation_matched_article_source"])
            if row.get("simulation_matched_article_source")
            else None
        ),
        simulation_matched_article_published_at=(
            _to_utc(row["simulation_matched_article_published_at"])
            if row.get("simulation_matched_article_published_at")
            else None
        ),
        production_filter_outcome=row["production_filter_outcome"],
        simulation_filter_outcome=row["simulation_filter_outcome"],
        rejection_reason_code=row["rejection_reason_code"],
        production_rejection_reason_code=row.get("production_rejection_reason_code"),
        simulation_rejection_reason_code=row.get("simulation_rejection_reason_code"),
        probable_cause=row["probable_cause"],
        improvement_suggestion=row["improvement_suggestion"],
        rationale=row["rationale"],
        classification_confidence=_optional_float(row["classification_confidence"]),
        suggestion_json=suggestion_json,
        recommended_include_keywords=normalize_keywords(_string_list(suggestion_json.get("recommended_include_keywords"))),
        evaluated_at=_to_utc(row["evaluated_at"]),
    )


def _fetched_article(row: dict[str, Any]) -> FetchedArticle:
    return FetchedArticle(
        article_id=str(row["article_id"]),
        source=str(row["source"]),
        source_key=str(row["source_key"]),
        headline=str(row["headline"]),
        summary=row["summary"],
        url=str(row["url"]),
        tickers=_string_list(row["tickers"]),
        published_at=_to_utc(row["published_at"]),
        fetched_at=_to_utc(row["fetched_at"]),
        filter_outcome=row.get("filter_outcome"),
        rejection_reason_code=row.get("rejection_reason_code"),
        matched_article_id=(
            str(row["matched_article_id"]) if row.get("matched_article_id") else None
        ),
    )


def _incorrectly_accepted_item(row: dict[str, Any]) -> FilterQualityIncorrectlyAcceptedItem:
    return FilterQualityIncorrectlyAcceptedItem(
        assessment_id=str(row["assessment_id"]),
        run_id=str(row["run_id"]),
        article_id=str(row["article_id"]),
        headline=str(row["headline"]),
        summary=row["summary"],
        url=str(row["url"]),
        source=str(row["source"]),
        published_at=_to_utc(row["published_at"]),
        production_filter_outcome=row["production_filter_outcome"],
        simulation_filter_outcome=row["simulation_filter_outcome"],
        probable_cause=row["probable_cause"],
        improvement_suggestion=row["improvement_suggestion"],
        rationale=row["rationale"],
        classification_confidence=_optional_float(row["classification_confidence"]),
        suggestion_json=dict(row["suggestion_json"] or {}),
        evaluated_at=_to_utc(row["evaluated_at"]),
    )


def _news_filter_config(row: dict[str, Any]) -> NewsFilterConfig:
    return NewsFilterConfig(
        filter_config_id=str(row["filter_config_id"]),
        config_name=str(row["config_name"]),
        config_role=str(row["config_role"]),
        status=str(row["status"]),
        include_keywords=normalize_keywords(_string_list(row["include_keywords"])),
        exclude_keywords=normalize_keywords(_string_list(row["exclude_keywords"])),
        watchlist_tickers=normalize_tickers(_string_list(row["watchlist_tickers"])),
        dedupe_algorithm=str(row["dedupe_algorithm"]),
        dedupe_similarity_threshold=float(row["dedupe_similarity_threshold"]),
        dedupe_lookback_hours=int(row["dedupe_lookback_hours"]),
        created_from_run_id=row["created_from_run_id"],
    )


def _filter_config_payload(config: NewsFilterConfig) -> NewsFilterConfigPayload:
    return NewsFilterConfigPayload(
        filter_config_id=config.filter_config_id,
        config_name=config.config_name,
        config_role=config.config_role,
        status=config.status,
        include_keywords=list(config.include_keywords),
        exclude_keywords=list(config.exclude_keywords),
        watchlist_tickers=list(config.watchlist_tickers),
        dedupe_algorithm=config.dedupe_algorithm,
        dedupe_similarity_threshold=config.dedupe_similarity_threshold,
        dedupe_lookback_hours=config.dedupe_lookback_hours,
        created_from_run_id=config.created_from_run_id,
    )


def _filter_config_payload_from_snapshot(
    snapshot: Any,
    *,
    created_from_run_id: str | None,
) -> NewsFilterConfigPayload | None:
    snapshot_dict = dict(snapshot or {})
    if not snapshot_dict:
        return None
    config = config_from_snapshot(
        snapshot_dict,
        filter_config_id="",
        config_name="Evaluated filter",
        config_role="test",
        status="active",
        created_from_run_id=created_from_run_id,
    )
    payload = _filter_config_payload(config)
    return payload.model_copy(update={"filter_config_id": None})


def _payload_snapshot(payload: NewsFilterConfigPayload) -> dict[str, Any]:
    return {
        "include_keywords": payload.include_keywords,
        "exclude_keywords": payload.exclude_keywords,
        "watchlist_tickers": payload.watchlist_tickers,
        "dedupe_algorithm": payload.dedupe_algorithm,
        "dedupe_similarity_threshold": payload.dedupe_similarity_threshold,
        "dedupe_lookback_hours": payload.dedupe_lookback_hours,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass(frozen=True)
class BacktestRunRow:
    run_id: str
    status: str
    window_start_at: datetime
    window_end_at: datetime
    mode: str
    timing_scenario: str
    card_population: str
    strategies_requested: list[str] | None
    initial_capital: float
    net_pnl: float | None
    total_return: float | None
    win_rate: float | None
    profit_factor: float | None
    max_drawdown: float | None
    created_at: datetime
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
    gross_profit: float | None
    gross_loss: float | None
    total_commission: float | None
    total_slippage: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None
    max_drawdown_duration_seconds: float | None
    sharpe_ratio: float | None
    exposure_fraction: float | None
    signal_accuracy: float | None
    cards_considered: int
    cards_in_population: int
    cards_live_executable: int
    cards_skipped_no_price: int
    trades_opened: int
    trades_closed: int
    trades_risk_blocked: int
    avg_news_fetch_delay_seconds: float | None
    p95_news_fetch_delay_seconds: float | None
    max_news_fetch_delay_seconds: float | None
    avg_thesis_build_delay_seconds: float | None
    p95_thesis_build_delay_seconds: float | None
    max_thesis_build_delay_seconds: float | None
    avg_total_pipeline_delay_seconds: float | None
    p95_total_pipeline_delay_seconds: float | None
    max_total_pipeline_delay_seconds: float | None
    pnl_gap: float | None
    win_rate_gap: float | None
    trades_flipped_by_delay: int | None
    llm_model: str | None
    llm_token_budget_limit: int | None
    llm_tokens_used: int | None
    budget_exhausted: bool | None
    analysis_coverage_until_at: datetime | None
    summary_json: dict[str, Any]


@dataclass(frozen=True)
class BacktestTradeRow:
    trade_id: str
    ticker: str
    exchange_code: str
    strategy: str
    direction: str
    entry_timing_scenario: str
    entry_at: datetime | None
    entry_price: float | None
    exit_at: datetime | None
    exit_price: float | None
    net_pnl: float | None
    return_pct: float | None
    exit_reason: str
    risk_block_rule: str | None
    news_fetch_delay_seconds: float | None
    thesis_build_delay_seconds: float | None
    total_pipeline_delay_seconds: float | None
    card_decision_state: str
    card_was_live_expired: bool


@dataclass(frozen=True)
class BacktestEquityRow:
    timing_scenario: str
    as_of: datetime
    equity: float
    open_positions: int


@dataclass(frozen=True)
class BacktestCardTradeRow:
    trade_id: str
    entry_timing_scenario: str
    entry_at: datetime | None
    entry_price: float | None
    exit_at: datetime | None
    exit_price: float | None
    net_pnl: float | None
    return_pct: float | None
    exit_reason: str | None
    risk_block_rule: str | None


@dataclass(frozen=True)
class BacktestCardRow:
    thesis_card_id: str
    ticker: str
    exchange_code: str
    direction: str
    strategy: str
    time_horizon: str | None
    confidence: float | None
    decision_state: str
    card_created_at: datetime
    card_expires_at: datetime | None
    trades: list[BacktestCardTradeRow]


def _backtest_run_row(row: dict[str, Any]) -> BacktestRunRow:
    strategies = row.get("strategies_requested")
    return BacktestRunRow(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        window_start_at=_to_utc(row["window_start_at"]),
        window_end_at=_to_utc(row["window_end_at"]),
        mode=str(row["mode"]),
        timing_scenario=str(row["timing_scenario"]),
        card_population=str(row["card_population"]),
        strategies_requested=list(strategies) if strategies is not None else None,
        initial_capital=float(row["initial_capital"]),
        net_pnl=_optional_float(row.get("net_pnl")),
        total_return=_optional_float(row.get("total_return")),
        win_rate=_optional_float(row.get("win_rate")),
        profit_factor=_optional_float(row.get("profit_factor")),
        max_drawdown=_optional_float(row.get("max_drawdown")),
        created_at=_to_utc(row["created_at"]),
        started_at=_to_utc(row["started_at"]),
        finished_at=_to_utc(row["finished_at"]) if row.get("finished_at") else None,
        error_code=row.get("error_code"),
        gross_profit=_optional_float(row.get("gross_profit")),
        gross_loss=_optional_float(row.get("gross_loss")),
        total_commission=_optional_float(row.get("total_commission")),
        total_slippage=_optional_float(row.get("total_slippage")),
        avg_win=_optional_float(row.get("avg_win")),
        avg_loss=_optional_float(row.get("avg_loss")),
        expectancy=_optional_float(row.get("expectancy")),
        max_drawdown_duration_seconds=_optional_float(row.get("max_drawdown_duration_seconds")),
        sharpe_ratio=_optional_float(row.get("sharpe_ratio")),
        exposure_fraction=_optional_float(row.get("exposure_fraction")),
        signal_accuracy=_optional_float(row.get("signal_accuracy")),
        cards_considered=int(row.get("cards_considered") or 0),
        cards_in_population=int(row.get("cards_in_population") or 0),
        cards_live_executable=int(row.get("cards_live_executable") or 0),
        cards_skipped_no_price=int(row.get("cards_skipped_no_price") or 0),
        trades_opened=int(row.get("trades_opened") or 0),
        trades_closed=int(row.get("trades_closed") or 0),
        trades_risk_blocked=int(row.get("trades_risk_blocked") or 0),
        avg_news_fetch_delay_seconds=_optional_float(row.get("avg_news_fetch_delay_seconds")),
        p95_news_fetch_delay_seconds=_optional_float(row.get("p95_news_fetch_delay_seconds")),
        max_news_fetch_delay_seconds=_optional_float(row.get("max_news_fetch_delay_seconds")),
        avg_thesis_build_delay_seconds=_optional_float(row.get("avg_thesis_build_delay_seconds")),
        p95_thesis_build_delay_seconds=_optional_float(row.get("p95_thesis_build_delay_seconds")),
        max_thesis_build_delay_seconds=_optional_float(row.get("max_thesis_build_delay_seconds")),
        avg_total_pipeline_delay_seconds=_optional_float(row.get("avg_total_pipeline_delay_seconds")),
        p95_total_pipeline_delay_seconds=_optional_float(row.get("p95_total_pipeline_delay_seconds")),
        max_total_pipeline_delay_seconds=_optional_float(row.get("max_total_pipeline_delay_seconds")),
        pnl_gap=_optional_float(row.get("pnl_gap")),
        win_rate_gap=_optional_float(row.get("win_rate_gap")),
        trades_flipped_by_delay=(
            int(row["trades_flipped_by_delay"])
            if row.get("trades_flipped_by_delay") is not None
            else None
        ),
        llm_model=row.get("llm_model"),
        llm_token_budget_limit=(
            int(row["llm_token_budget_limit"])
            if row.get("llm_token_budget_limit") is not None
            else None
        ),
        llm_tokens_used=(
            int(row["llm_tokens_used"]) if row.get("llm_tokens_used") is not None else None
        ),
        budget_exhausted=(
            bool(row["budget_exhausted"]) if row.get("budget_exhausted") is not None else None
        ),
        analysis_coverage_until_at=(
            _to_utc(row["analysis_coverage_until_at"])
            if row.get("analysis_coverage_until_at")
            else None
        ),
        summary_json=dict(row.get("summary_json") or {}),
    )


def _backtest_trade_row(row: dict[str, Any]) -> BacktestTradeRow:
    return BacktestTradeRow(
        trade_id=str(row["trade_id"]),
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        strategy=str(row["strategy"]),
        direction=str(row["direction"]),
        entry_timing_scenario=str(row["entry_timing_scenario"]),
        entry_at=_to_utc(row["entry_at"]) if row.get("entry_at") else None,
        entry_price=_optional_float(row.get("entry_price")),
        exit_at=_to_utc(row["exit_at"]) if row.get("exit_at") else None,
        exit_price=_optional_float(row.get("exit_price")),
        net_pnl=_optional_float(row.get("net_pnl")),
        return_pct=_optional_float(row.get("return_pct")),
        exit_reason=str(row["exit_reason"]),
        risk_block_rule=row.get("risk_block_rule"),
        news_fetch_delay_seconds=_optional_float(row.get("news_fetch_delay_seconds")),
        thesis_build_delay_seconds=_optional_float(row.get("thesis_build_delay_seconds")),
        total_pipeline_delay_seconds=_optional_float(row.get("total_pipeline_delay_seconds")),
        card_decision_state=str(row["card_decision_state"]),
        card_was_live_expired=bool(row.get("card_was_live_expired")),
    )


def _backtest_equity_row(row: dict[str, Any]) -> BacktestEquityRow:
    return BacktestEquityRow(
        timing_scenario=str(row["timing_scenario"]),
        as_of=_to_utc(row["as_of"]),
        equity=float(row["equity"]),
        open_positions=int(row.get("open_positions") or 0),
    )


def _group_backtest_cards(rows: list[dict[str, Any]]) -> list[BacktestCardRow]:
    seen: dict[str, BacktestCardRow] = {}
    order: list[str] = []
    for row in rows:
        cid = str(row["thesis_card_id"])
        if cid not in seen:
            seen[cid] = BacktestCardRow(
                thesis_card_id=cid,
                ticker=str(row["ticker"]),
                exchange_code=str(row["exchange_code"]),
                direction=str(row["direction"]),
                strategy=str(row["strategy"]),
                time_horizon=str(row["time_horizon"]) if row.get("time_horizon") else None,
                confidence=_optional_float(row.get("confidence")),
                decision_state=str(row["decision_state"]),
                card_created_at=_to_utc(row["card_created_at"]),
                card_expires_at=_to_utc(row["card_expires_at"]) if row.get("card_expires_at") else None,
                trades=[],
            )
            order.append(cid)
        if row.get("trade_id") is not None:
            trade = BacktestCardTradeRow(
                trade_id=str(row["trade_id"]),
                entry_timing_scenario=str(row["entry_timing_scenario"]),
                entry_at=_to_utc(row["entry_at"]) if row.get("entry_at") else None,
                entry_price=_optional_float(row.get("entry_price")),
                exit_at=_to_utc(row["exit_at"]) if row.get("exit_at") else None,
                exit_price=_optional_float(row.get("exit_price")),
                net_pnl=_optional_float(row.get("net_pnl")),
                return_pct=_optional_float(row.get("return_pct")),
                exit_reason=str(row["exit_reason"]) if row.get("exit_reason") else None,
                risk_block_rule=row.get("risk_block_rule"),
            )
            # BacktestCardRow is frozen so we rebuild with the appended trade
            existing = seen[cid]
            seen[cid] = BacktestCardRow(
                thesis_card_id=existing.thesis_card_id,
                ticker=existing.ticker,
                exchange_code=existing.exchange_code,
                direction=existing.direction,
                strategy=existing.strategy,
                time_horizon=existing.time_horizon,
                confidence=existing.confidence,
                decision_state=existing.decision_state,
                card_created_at=existing.card_created_at,
                card_expires_at=existing.card_expires_at,
                trades=[*existing.trades, trade],
            )
    return [seen[cid] for cid in order]
