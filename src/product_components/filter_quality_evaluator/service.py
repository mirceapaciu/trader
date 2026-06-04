from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.core_components.event_ingestion_engine.models import FilterRun, FilterRunMode
from src.product_components.news_fetcher.settings import NewsFetcherSettings

from .llm_classifier import LlmClassifier, OpenAIResponsesClient, TokenBudgetExhausted
from .models import (
    ClassificationResult,
    ComparisonItem,
    EvaluationScope,
    FilterQualityRunParams,
    ItemAssessment,
    ItemStatus,
)
from .repository import FilterQualityRepository, dataset_snapshot_hash
from .settings import FilterQualityEvaluatorSettings
from .simulation import (
    deterministic_sample,
    fingerprint_for_snapshot,
    simulate_filter_results,
    simulation_config_from_news_settings,
    simulation_config_from_snapshot,
)
from .summary import build_run_summary


class FilterQualityEvaluatorService:
    def __init__(
        self,
        *,
        settings: FilterQualityEvaluatorSettings,
        news_settings: NewsFetcherSettings,
        repository: FilterQualityRepository,
        classifier: LlmClassifier | None = None,
    ) -> None:
        self._settings = settings
        self._news_settings = news_settings
        self._repository = repository
        self._classifier = classifier or LlmClassifier(
            client=OpenAIResponsesClient(),
            model=settings.llm_model,
            max_tokens_per_run=settings.llm_max_tokens_per_run,
            max_tokens_per_item=settings.llm_max_tokens_per_item,
            min_confidence_threshold=settings.min_confidence_threshold,
        )

    def run(self, params: FilterQualityRunParams) -> None:
        self._validate_params(params)
        articles = self._repository.load_input_articles(
            window_start_at=params.news_window_start_at,
            window_end_at=params.news_window_end_at,
        )
        snapshot_hash = dataset_snapshot_hash(articles)
        self._repository.create_run(
            run_id=params.run_id,
            news_window_start_at=params.news_window_start_at,
            news_window_end_at=params.news_window_end_at,
            dataset_snapshot_hash=snapshot_hash,
            filter_config_fingerprint=params.filter_config_fingerprint,
            run_note=params.run_note,
            accepted_audit_enabled=params.accepted_audit_enabled,
            accepted_audit_sample_size=params.accepted_audit_sample_size,
            token_budget_limit=self._settings.llm_max_tokens_per_run,
        )

        try:
            if len(articles) > self._settings.max_items_per_run:
                raise ValueError("dataset_exceeds_max_items_per_run")

            watchlist = self._repository.load_active_watchlist_tickers(
                shared_schema=self._settings.shared_db_schema,
                watchlist_table=self._settings.watchlist_table,
            )
            if params.filter_config_snapshot_json:
                simulation_config = simulation_config_from_snapshot(params.filter_config_snapshot_json)
            else:
                simulation_config = simulation_config_from_news_settings(
                    self._news_settings,
                    watchlist_tickers=watchlist,
                )
            snapshot = simulation_config.snapshot()
            fingerprint = params.filter_config_fingerprint or fingerprint_for_snapshot(snapshot)
            production_filter_run_id = self._repository.resolve_production_filter_run_id(
                filter_config_fingerprint=params.filter_config_fingerprint,
                window_start_at=params.news_window_start_at,
                window_end_at=params.news_window_end_at,
            )
            simulation_filter_run_id = f"sim_{params.run_id}"
            simulation_run = FilterRun(
                filter_run_id=simulation_filter_run_id,
                run_mode=FilterRunMode.SIMULATION,
                filter_config_fingerprint=fingerprint,
                filter_config_snapshot_json=snapshot,
                run_note=params.run_note,
                window_start_at=params.news_window_start_at,
                window_end_at=params.news_window_end_at,
            )
            self._repository.persist_simulation_results(
                filter_run=simulation_run,
                results=simulate_filter_results(articles, config=simulation_config),
            )

            comparisons = self._repository.load_comparison_items(
                articles=articles,
                production_filter_run_id=production_filter_run_id,
                simulation_filter_run_id=simulation_filter_run_id,
            )
            assessments = self._evaluate_items(
                params=params,
                comparisons=comparisons,
                filter_config_snapshot_json=snapshot,
            )
            dataset_rejected_count = sum(
                1
                for item in comparisons
                if item.simulation_result is not None and item.simulation_result.outcome.value == "rejected"
            )
            dataset_accepted_count = sum(
                1
                for item in comparisons
                if item.simulation_result is not None and item.simulation_result.outcome.value == "accepted"
            )
            summary = build_run_summary(
                dataset_input_count=len(articles),
                dataset_rejected_count=dataset_rejected_count,
                dataset_accepted_count=dataset_accepted_count,
                accepted_audit_enabled=params.accepted_audit_enabled,
                accepted_audit_sample_size=params.accepted_audit_sample_size,
                assessments=assessments,
            )
            self._repository.finalize_run_success(run_id=params.run_id, summary=summary)
        except TokenBudgetExhausted:
            self._repository.finalize_run_failure(run_id=params.run_id, error_code="token_budget_exhausted")
            raise
        except Exception as error:
            self._repository.finalize_run_failure(
                run_id=params.run_id,
                error_code=str(error) or error.__class__.__name__,
                error_details_json={"exception_type": error.__class__.__name__},
            )
            raise

    def _evaluate_items(
        self,
        *,
        params: FilterQualityRunParams,
        comparisons: list[ComparisonItem],
        filter_config_snapshot_json: dict[str, Any],
    ) -> list[ItemAssessment]:
        assessments: list[ItemAssessment] = []
        for item in comparisons:
            if item.production_result is None:
                assessments.append(self._failed_assessment(params.run_id, item, "missing_production_result"))
            elif item.simulation_result is None:
                assessments.append(self._failed_assessment(params.run_id, item, "missing_simulation_result"))

        rejected = [
            item
            for item in comparisons
            if item.is_complete and item.simulation_result.outcome.value == "rejected"
        ]
        rejected = sorted(rejected, key=lambda item: (not item.is_disagreement, item.article.id))
        accepted = [
            item
            for item in comparisons
            if item.is_complete and item.simulation_result.outcome.value == "accepted"
        ]
        if params.accepted_audit_enabled:
            sample_size = params.accepted_audit_sample_size or self._settings.accepted_audit_sample_size
            accepted_sample = deterministic_sample(accepted, run_id=params.run_id, sample_size=sample_size)
        else:
            accepted_sample = []

        for item in rejected:
            assessments.append(
                self._classify_or_fail(
                    params.run_id,
                    item,
                    EvaluationScope.REJECTED_POPULATION,
                    filter_config_snapshot_json,
                )
            )
        for item in accepted_sample:
            assessments.append(
                self._classify_or_fail(
                    params.run_id,
                    item,
                    EvaluationScope.ACCEPTED_AUDIT,
                    filter_config_snapshot_json,
                )
            )

        for assessment in assessments:
            self._repository.insert_item_assessment(assessment)
        return assessments

    def _classify_or_fail(
        self,
        run_id: str,
        item: ComparisonItem,
        scope: EvaluationScope,
        filter_config_snapshot_json: dict[str, Any],
    ) -> ItemAssessment:
        try:
            result = self._classifier.classify_item(
                item=item,
                scope=scope,
                filter_config_snapshot_json=filter_config_snapshot_json,
            )
        except TokenBudgetExhausted:
            raise
        except Exception as error:
            return self._failed_assessment(
                run_id,
                item,
                "llm_classification_failed",
                {"exception_type": error.__class__.__name__},
            )
        return self._evaluated_assessment(run_id, item, scope, result)

    def _evaluated_assessment(
        self,
        run_id: str,
        item: ComparisonItem,
        scope: EvaluationScope,
        result: ClassificationResult,
    ) -> ItemAssessment:
        simulation_reason = item.simulation_result.rejection_reason_code
        return ItemAssessment(
            assessment_id=_assessment_id(run_id, item.article.id),
            run_id=run_id,
            article_id=item.article.id,
            evaluation_scope=scope,
            source=item.article.source,
            published_at=item.article.published_at,
            filter_run_id_production=item.filter_run_id_production,
            filter_run_id_simulation=item.filter_run_id_simulation,
            production_filter_outcome=item.production_result.outcome.value,
            simulation_filter_outcome=item.simulation_result.outcome.value,
            is_disagreement=item.is_disagreement,
            rejection_reason_code=simulation_reason if item.simulation_result.outcome.value == "rejected" else None,
            item_status=ItemStatus.EVALUATED,
            classification_label=result.classification_label,
            classification_confidence=result.classification_confidence,
            rationale=result.rationale,
            probable_cause=result.probable_cause,
            improvement_suggestion=result.improvement_suggestion,
            suggestion_json=result.suggestion_json,
            llm_model=result.llm_model,
        )

    def _failed_assessment(
        self,
        run_id: str,
        item: ComparisonItem,
        error_code: str,
        error_details_json: dict[str, Any] | None = None,
    ) -> ItemAssessment:
        return ItemAssessment(
            assessment_id=_assessment_id(run_id, item.article.id),
            run_id=run_id,
            article_id=item.article.id,
            evaluation_scope=EvaluationScope.REJECTED_POPULATION,
            source=item.article.source,
            published_at=item.article.published_at,
            filter_run_id_production=item.filter_run_id_production,
            filter_run_id_simulation=item.filter_run_id_simulation,
            production_filter_outcome=(
                item.production_result.outcome.value if item.production_result else None
            ),
            simulation_filter_outcome=(
                item.simulation_result.outcome.value if item.simulation_result else None
            ),
            is_disagreement=item.is_disagreement,
            rejection_reason_code=(
                item.simulation_result.rejection_reason_code
                if item.simulation_result and item.simulation_result.outcome.value == "rejected"
                else None
            ),
            item_status=ItemStatus.FAILED,
            item_error_code=error_code,
            item_error_details_json=error_details_json or {},
        )

    def _validate_params(self, params: FilterQualityRunParams) -> None:
        if params.news_window_start_at >= params.news_window_end_at:
            raise ValueError("invalid_news_window")
        if params.accepted_audit_enabled and not params.accepted_audit_sample_size:
            raise ValueError("accepted_audit_sample_size_required")
        if params.accepted_audit_sample_size is not None and params.accepted_audit_sample_size <= 0:
            raise ValueError("invalid_accepted_audit_sample_size")


def new_run_id() -> str:
    return f"fqe_{uuid.uuid4().hex}"


def _assessment_id(run_id: str, article_id: str) -> str:
    return f"fqa_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{article_id}').hex}"
