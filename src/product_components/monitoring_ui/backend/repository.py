from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import errors
import redis
from psycopg.rows import dict_row

from .models import (
    BacklogResponse,
    DeadLetterItem,
    DeadLetterResponse,
    DependencyHealth,
    FilterQualityIncorrectlyRejectedItem,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityRunSummary,
    FilterQualityStatusResponse,
    ProvidersResponse,
    ProviderStatus,
    ThroughputBucket,
    ThroughputResponse,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresRedisMonitoringDataSource:
    def __init__(
        self,
        *,
        dsn: str,
        news_schema: str,
        filter_quality_schema: str,
        queue_url: str,
        news_raw_queue: str,
        query_timeout_seconds: int,
    ) -> None:
        self._dsn = dsn
        self._news_schema = _safe_identifier(news_schema)
        self._filter_quality_schema = _safe_identifier(filter_quality_schema)
        self._queue_url = queue_url
        self._news_raw_queue = news_raw_queue
        self._query_timeout_seconds = query_timeout_seconds

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

    def list_providers(self) -> ProvidersResponse:
        sql = (
            f"SELECT source_key, last_cycle_started_at, last_cycle_finished_at, "
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
                    publish_success_count=self._count_published(row["source_key"]),
                    fetch_count=int(row["last_cycle_fetched_count"]),
                    fetch_error_count=1 if row["last_cycle_error_code"] else 0,
                    persist_success_count=int(row["last_cycle_accepted_count"]),
                    last_error_code=row["last_cycle_error_code"],
                    last_error_at=last_cycle_end if row["last_cycle_error_code"] else None,
                )
            )

        return ProvidersResponse(providers=providers, generated_at=_utc_now())

    def get_throughput(self, *, window: str) -> ThroughputResponse:
        interval = _window_to_interval(window)
        sql = (
            f"SELECT date_trunc('minute', created_at) AS window_start, source_key, "
            f"COUNT(*) FILTER (WHERE status IN ('pending', 'publishing', 'published', 'dead_lettered')) AS fetch_count, "
            f"COUNT(*) FILTER (WHERE status = 'published') AS publish_success_count, "
            f"COUNT(*) FILTER (WHERE status = 'dead_lettered') AS publish_error_count "
            f"FROM {self._news_schema}.t_publication_obligations "
            f"WHERE created_at >= NOW() - %s::interval "
            f"GROUP BY 1, 2 ORDER BY 1, 2"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (interval,))
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
        return ThroughputResponse(window=window, buckets=buckets, generated_at=_utc_now())

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
            f"SELECT a.assessment_id, a.run_id, a.article_id, i.headline, i.url, a.source, a.published_at, "
            f"a.production_filter_outcome, a.simulation_filter_outcome, a.rejection_reason_code, "
            f"a.probable_cause, a.improvement_suggestion, a.rationale, a.classification_confidence, "
            f"a.suggestion_json, a.evaluated_at "
            f"FROM {self._filter_quality_schema}.t_filter_quality_item_assessments a "
            f"JOIN {self._news_schema}.t_input_news_articles i ON i.id = a.article_id "
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

    def get_running_filter_quality_run(self) -> FilterQualityRunSummary | None:
        sql = (
            f"SELECT {_filter_quality_run_columns(self._filter_quality_schema)} "
            f"FROM {self._filter_quality_schema}.t_filter_quality_runs r "
            f"WHERE r.status = 'running' "
            f"ORDER BY r.created_at DESC LIMIT 1"
        )
        return self._fetch_filter_quality_run(sql, ())

    def get_last_filter_quality_run(self) -> FilterQualityRunSummary | None:
        sql = (
            f"SELECT {_filter_quality_run_columns(self._filter_quality_schema)} "
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


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _window_to_interval(window: str) -> str:
    normalized = window.strip().lower()
    allowed = {"15m": "15 minutes", "1h": "1 hour", "24h": "24 hours"}
    return allowed.get(normalized, "1 hour")


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _filter_quality_run_columns(schema: str) -> str:
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
        "r.summary_json, r.recommendation_summary_md"
    )


def _filter_quality_run(row: dict[str, Any]) -> FilterQualityRunSummary:
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
        dataset_input_count=int(row["dataset_input_count"]),
        dataset_rejected_count=int(row["dataset_rejected_count"]),
        dataset_accepted_count=int(row["dataset_accepted_count"]),
        rejected_items_evaluated=int(row["rejected_items_evaluated"]),
        accepted_items_sampled=int(row["accepted_items_sampled"]),
        correctly_rejected_count=int(row["correctly_rejected_count"]),
        incorrectly_rejected_count=int(row["incorrectly_rejected_count"]),
        correctly_accepted_count=int(row["correctly_accepted_count"]),
        incorrectly_accepted_count=int(row["incorrectly_accepted_count"]),
        item_failed_count=int(row["item_failed_count"] or 0),
        item_error_codes={str(key): int(value) for key, value in dict(row["item_error_codes"] or {}).items()},
        summary_json=dict(row["summary_json"] or {}),
        recommendation_summary_md=str(row["recommendation_summary_md"] or ""),
    )


def _incorrectly_rejected_item(row: dict[str, Any]) -> FilterQualityIncorrectlyRejectedItem:
    return FilterQualityIncorrectlyRejectedItem(
        assessment_id=str(row["assessment_id"]),
        run_id=str(row["run_id"]),
        article_id=str(row["article_id"]),
        headline=str(row["headline"]),
        url=str(row["url"]),
        source=str(row["source"]),
        published_at=_to_utc(row["published_at"]),
        production_filter_outcome=row["production_filter_outcome"],
        simulation_filter_outcome=row["simulation_filter_outcome"],
        rejection_reason_code=row["rejection_reason_code"],
        probable_cause=row["probable_cause"],
        improvement_suggestion=row["improvement_suggestion"],
        rationale=row["rationale"],
        classification_confidence=_optional_float(row["classification_confidence"]),
        suggestion_json=dict(row["suggestion_json"] or {}),
        evaluated_at=_to_utc(row["evaluated_at"]),
    )
