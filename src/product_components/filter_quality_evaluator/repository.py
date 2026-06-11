from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.core_components.event_ingestion_engine.models import (
    FilterOutcome,
    FilterResult,
    FilterRun,
)

from .models import ComparisonItem, InputArticle, ItemAssessment, RunStatus, RunSummary

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FilterQualityRepository:
    def __init__(self, *, dsn: str, evaluator_schema: str, news_schema: str) -> None:
        self._dsn = dsn
        self._evaluator_schema = _safe_identifier(evaluator_schema)
        self._news_schema = _safe_identifier(news_schema)

    def bootstrap_schema(self, *, repo_root: Path) -> None:
        schema_files = (
            repo_root / "src" / "product_components" / "shared" / "db" / "schema.sql",
            repo_root / "src" / "product_components" / "news_fetcher" / "db" / "schema.sql",
            repo_root / "src" / "product_components" / "filter_quality_evaluator" / "db" / "schema.sql",
        )
        with closing(psycopg.connect(self._dsn)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                for schema_file in schema_files:
                    cursor.execute(schema_file.read_text(encoding="utf-8"))

    def load_active_watchlist_tickers(self, *, shared_schema: str, watchlist_table: str) -> set[str]:
        safe_shared = _safe_identifier(shared_schema)
        safe_table = _safe_identifier(watchlist_table)
        sql = f"SELECT ticker FROM {safe_shared}.{safe_table} WHERE is_active = TRUE"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            return {str(row[0]).strip().upper() for row in cur.fetchall() if str(row[0]).strip()}

    def load_input_articles(self, *, window_start_at: datetime, window_end_at: datetime) -> list[InputArticle]:
        sql = (
            f"SELECT id, source, headline, summary, url, tickers, published_at, fetched_at, sentiment_source "
            f"FROM {self._news_schema}.t_input_news_articles "
            f"WHERE published_at >= %s AND published_at < %s "
            f"ORDER BY published_at, id"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (_to_utc(window_start_at), _to_utc(window_end_at)))
            rows = cur.fetchall()
        return [_input_article(row) for row in rows]

    def create_run(
        self,
        *,
        run_id: str,
        news_window_start_at: datetime,
        news_window_end_at: datetime,
        dataset_snapshot_hash: str,
        filter_config_fingerprint: str | None,
        run_note: str | None,
        accepted_audit_enabled: bool,
        accepted_audit_sample_size: int | None,
        token_budget_limit: int,
    ) -> None:
        sql = (
            f"INSERT INTO {self._evaluator_schema}.t_filter_quality_runs "
            f"(run_id, news_window_start_at, news_window_end_at, dataset_snapshot_hash, "
            f"filter_config_fingerprint, trigger_mode, run_note, accepted_audit_enabled, "
            f"accepted_audit_sample_size, status, token_budget_limit) "
            f"VALUES (%s, %s, %s, %s, %s, 'manual_cli', %s, %s, %s, 'running', %s)"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    run_id,
                    _to_utc(news_window_start_at),
                    _to_utc(news_window_end_at),
                    dataset_snapshot_hash,
                    filter_config_fingerprint,
                    run_note,
                    accepted_audit_enabled,
                    accepted_audit_sample_size,
                    token_budget_limit,
                ),
            )
            conn.commit()

    def resolve_production_filter_run_id(
        self,
        *,
        filter_config_fingerprint: str | None,
        window_start_at: datetime,
        window_end_at: datetime,
    ) -> str:
        if filter_config_fingerprint:
            sql = (
                f"SELECT filter_run_id FROM {self._news_schema}.t_news_filter_runs "
                f"WHERE run_mode = 'production' AND filter_config_fingerprint = %s "
                f"ORDER BY created_at DESC LIMIT 2"
            )
            params = (filter_config_fingerprint,)
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            if not rows:
                raise ValueError("missing_production_filter_run")
            if len(rows) > 1:
                raise ValueError("ambiguous_production_filter_run")
            return str(rows[0][0])
        else:
            sql = (
                f"SELECT filter_run_id FROM {self._news_schema}.t_news_filter_runs "
                f"WHERE run_mode = 'production' "
                f"AND (window_start_at IS NULL OR window_start_at <= %s) "
                f"AND (window_end_at IS NULL OR window_end_at >= %s) "
                f"ORDER BY created_at DESC LIMIT 1"
            )
            params = (_to_utc(window_start_at), _to_utc(window_end_at))

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            raise ValueError("missing_production_filter_run")
        return str(row[0])

    def load_filter_run_snapshot(self, *, filter_run_id: str) -> dict[str, Any]:
        sql = (
            f"SELECT filter_config_snapshot_json FROM {self._news_schema}.t_news_filter_runs "
            f"WHERE filter_run_id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (filter_run_id,))
            row = cur.fetchone()
        if row is None:
            raise ValueError("missing_filter_run_snapshot")
        snapshot = dict(row[0] or {})
        if not snapshot:
            raise ValueError("empty_filter_run_snapshot")
        return snapshot

    def persist_simulation_results(
        self,
        *,
        filter_run: FilterRun,
        results: list[FilterResult],
    ) -> None:
        run_sql = (
            f"INSERT INTO {self._news_schema}.t_news_filter_runs "
            f"(filter_run_id, run_mode, filter_config_fingerprint, filter_config_snapshot_json, "
            f"run_note, window_start_at, window_end_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (filter_run_id) DO UPDATE SET "
            f"run_mode = EXCLUDED.run_mode, "
            f"filter_config_fingerprint = EXCLUDED.filter_config_fingerprint, "
            f"filter_config_snapshot_json = EXCLUDED.filter_config_snapshot_json, "
            f"run_note = EXCLUDED.run_note, "
            f"window_start_at = EXCLUDED.window_start_at, "
            f"window_end_at = EXCLUDED.window_end_at, "
            f"updated_at = NOW()"
        )
        result_sql = (
            f"INSERT INTO {self._news_schema}.t_news_filter_results "
            f"(filter_run_id, article_id, filter_outcome, rejection_reason_code, "
            f"matched_article_id, similarity_score, details_json) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (filter_run_id, article_id) DO NOTHING"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                run_sql,
                (
                    filter_run.filter_run_id,
                    filter_run.run_mode.value,
                    filter_run.filter_config_fingerprint,
                    Json(filter_run.filter_config_snapshot_json),
                    filter_run.run_note,
                    filter_run.window_start_at,
                    filter_run.window_end_at,
                ),
            )
            for result in results:
                cur.execute(
                    result_sql,
                    (
                        filter_run.filter_run_id,
                        result.article_id,
                        result.outcome.value,
                        result.rejection_reason_code,
                        result.matched_article_id,
                        result.similarity_score,
                        Json(result.details),
                    ),
                )
            conn.commit()

    def load_comparison_items(
        self,
        *,
        articles: list[InputArticle],
        production_filter_run_id: str | None,
        simulation_filter_run_id: str,
    ) -> list[ComparisonItem]:
        article_by_id = {article.id: article for article in articles}
        if production_filter_run_id is None:
            production = self._load_latest_production_filter_results(article_ids=list(article_by_id))
        else:
            production = {
                article_id: (production_filter_run_id, result)
                for article_id, result in self._load_filter_results(
                    filter_run_id=production_filter_run_id,
                    article_ids=list(article_by_id),
                ).items()
            }
        simulation = self._load_filter_results(
            filter_run_id=simulation_filter_run_id,
            article_ids=list(article_by_id),
        )
        return [
            ComparisonItem(
                article=article,
                filter_run_id_production=production[article.id][0] if article.id in production else "",
                filter_run_id_simulation=simulation_filter_run_id,
                production_result=production[article.id][1] if article.id in production else None,
                simulation_result=simulation.get(article.id),
            )
            for article in articles
        ]

    def insert_item_assessment(self, assessment: ItemAssessment) -> None:
        sql = (
            f"INSERT INTO {self._evaluator_schema}.t_filter_quality_item_assessments "
            f"(assessment_id, run_id, article_id, evaluation_scope, source, published_at, "
            f"filter_run_id_production, filter_run_id_simulation, production_filter_outcome, "
            f"simulation_filter_outcome, is_disagreement, rejection_reason_code, item_status, "
            f"item_error_code, item_error_details_json, classification_label, classification_confidence, "
            f"rationale, probable_cause, improvement_suggestion, suggestion_json, llm_model) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (run_id, article_id) DO NOTHING"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    assessment.assessment_id,
                    assessment.run_id,
                    assessment.article_id,
                    assessment.evaluation_scope.value,
                    assessment.source,
                    _to_utc(assessment.published_at),
                    assessment.filter_run_id_production,
                    assessment.filter_run_id_simulation,
                    assessment.production_filter_outcome,
                    assessment.simulation_filter_outcome,
                    assessment.is_disagreement,
                    assessment.rejection_reason_code,
                    assessment.item_status.value,
                    assessment.item_error_code,
                    Json(assessment.item_error_details_json),
                    assessment.classification_label.value if assessment.classification_label else None,
                    assessment.classification_confidence,
                    assessment.rationale,
                    assessment.probable_cause.value if assessment.probable_cause else None,
                    assessment.improvement_suggestion,
                    Json(assessment.suggestion_json),
                    assessment.llm_model,
                ),
            )
            conn.commit()

    def finalize_run_success(self, *, run_id: str, summary: RunSummary) -> None:
        sql = (
            f"UPDATE {self._evaluator_schema}.t_filter_quality_runs SET "
            f"status = 'completed', finished_at = NOW(), error_code = NULL, error_details_json = '{{}}'::jsonb, "
            f"dataset_input_count = %s, dataset_rejected_count = %s, dataset_accepted_count = %s, "
            f"rejected_items_evaluated = %s, accepted_items_sampled = %s, "
            f"correctly_rejected_count = %s, incorrectly_rejected_count = %s, "
            f"correctly_accepted_count = %s, incorrectly_accepted_count = %s, "
            f"rejection_precision_proxy = %s, incorrectly_accepted_rate_estimate = %s, "
            f"summary_json = %s, recommendation_summary_md = %s "
            f"WHERE run_id = %s"
        )
        self._update_run(sql, _summary_params(summary) + (run_id,))

    def finalize_run_failure(
        self,
        *,
        run_id: str,
        error_code: str,
        error_details_json: dict[str, Any] | None = None,
    ) -> None:
        sql = (
            f"UPDATE {self._evaluator_schema}.t_filter_quality_runs SET "
            f"status = 'failed', finished_at = NOW(), error_code = %s, error_details_json = %s "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (error_code, Json(error_details_json or {}), run_id))
            conn.commit()

    def _load_filter_results(
        self,
        *,
        filter_run_id: str,
        article_ids: list[str],
    ) -> dict[str, FilterResult]:
        if not article_ids:
            return {}
        sql = (
            f"SELECT article_id, filter_outcome, rejection_reason_code, matched_article_id, "
            f"similarity_score, details_json "
            f"FROM {self._news_schema}.t_news_filter_results "
            f"WHERE filter_run_id = %s AND article_id = ANY(%s)"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (filter_run_id, article_ids))
            rows = cur.fetchall()
        return {
            str(row["article_id"]): FilterResult(
                article_id=str(row["article_id"]),
                outcome=FilterOutcome(str(row["filter_outcome"])),
                rejection_reason_code=row["rejection_reason_code"],
                matched_article_id=row["matched_article_id"],
                similarity_score=row["similarity_score"],
                details=dict(row["details_json"] or {}),
            )
            for row in rows
        }

    def _load_latest_production_filter_results(
        self,
        *,
        article_ids: list[str],
    ) -> dict[str, tuple[str, FilterResult]]:
        if not article_ids:
            return {}
        sql = (
            f"SELECT DISTINCT ON (r.article_id) "
            f"r.filter_run_id, r.article_id, r.filter_outcome, r.rejection_reason_code, "
            f"r.matched_article_id, r.similarity_score, r.details_json "
            f"FROM {self._news_schema}.t_news_filter_results r "
            f"JOIN {self._news_schema}.t_news_filter_runs fr "
            f"ON fr.filter_run_id = r.filter_run_id "
            f"WHERE fr.run_mode = 'production' AND r.article_id = ANY(%s) "
            f"ORDER BY r.article_id, r.created_at DESC, fr.updated_at DESC, fr.created_at DESC, r.filter_run_id DESC"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (article_ids,))
            rows = cur.fetchall()
        return {
            str(row["article_id"]): (
                str(row["filter_run_id"]),
                FilterResult(
                    article_id=str(row["article_id"]),
                    outcome=FilterOutcome(str(row["filter_outcome"])),
                    rejection_reason_code=row["rejection_reason_code"],
                    matched_article_id=row["matched_article_id"],
                    similarity_score=row["similarity_score"],
                    details=dict(row["details_json"] or {}),
                ),
            )
            for row in rows
        }

    def _update_run(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def dataset_snapshot_hash(articles: list[InputArticle]) -> str:
    payload = [
        {
            "id": article.id,
            "published_at": _to_utc(article.published_at).isoformat(),
        }
        for article in sorted(articles, key=lambda item: (item.published_at, item.id))
    ]
    normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _summary_params(summary: RunSummary) -> tuple[Any, ...]:
    return (
        summary.dataset_input_count,
        summary.dataset_rejected_count,
        summary.dataset_accepted_count,
        summary.rejected_items_evaluated,
        summary.accepted_items_sampled,
        summary.correctly_rejected_count,
        summary.incorrectly_rejected_count,
        summary.correctly_accepted_count,
        summary.incorrectly_accepted_count,
        summary.rejection_precision_proxy,
        summary.incorrectly_accepted_rate_estimate,
        Json(summary.summary_json),
        summary.recommendation_summary_md,
    )


def _input_article(row: dict[str, Any]) -> InputArticle:
    tickers = row.get("tickers") or []
    return InputArticle(
        id=str(row["id"]),
        source=str(row["source"]),
        headline=str(row["headline"]),
        summary=row["summary"],
        url=str(row["url"]),
        tickers=[str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()],
        published_at=_to_utc(row["published_at"]),
        fetched_at=_to_utc(row["fetched_at"]),
        sentiment_source=row["sentiment_source"],
    )


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
