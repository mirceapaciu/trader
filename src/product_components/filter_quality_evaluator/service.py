from __future__ import annotations

import uuid
import re
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.core_components.event_ingestion_engine.models import FilterRun, FilterRunMode
from src.core_components.event_ingestion_engine.canonicalization import normalize_canonical_locator
from src.product_components.news_fetcher.settings import NewsFetcherSettings

from .llm_classifier import LlmClassifier, OpenAIResponsesClient, TokenBudgetExhausted
from .models import (
    ClassificationLabel,
    ClassificationResult,
    ComparisonItem,
    EvaluationScope,
    FilterQualityRunParams,
    ItemAssessment,
    ItemStatus,
    ProbableCause,
)
from .repository import FilterQualityRepository, dataset_snapshot_hash
from .settings import FilterQualityEvaluatorSettings
from .simulation import (
    deterministic_sample,
    fingerprint_for_snapshot,
    simulate_filter_results,
    simulation_config_from_snapshot,
)
from .summary import build_run_summary

_MAX_CONSECUTIVE_CLASSIFICATION_FAILURES = 3


class RepeatedLlmClassificationFailure(RuntimeError):
    def __init__(self, *, failure_count: int) -> None:
        super().__init__("repeated_llm_classification_failed")
        self.failure_count = failure_count


@dataclass(frozen=True)
class _EvaluationTask:
    item: ComparisonItem
    scope: EvaluationScope


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

            production_filter_run_id = None
            if params.filter_config_snapshot_json:
                simulation_config = simulation_config_from_snapshot(params.filter_config_snapshot_json)
                if params.filter_config_fingerprint:
                    production_filter_run_id = self._repository.resolve_production_filter_run_id(
                        filter_config_fingerprint=params.filter_config_fingerprint,
                        window_start_at=params.news_window_start_at,
                        window_end_at=params.news_window_end_at,
                    )
            else:
                production_filter_run_id = self._repository.resolve_production_filter_run_id(
                    filter_config_fingerprint=params.filter_config_fingerprint,
                    window_start_at=params.news_window_start_at,
                    window_end_at=params.news_window_end_at,
                )
                production_snapshot = self._repository.load_filter_run_snapshot(
                    filter_run_id=production_filter_run_id,
                )
                simulation_config = simulation_config_from_snapshot(production_snapshot)
            snapshot = simulation_config.snapshot()
            fingerprint = params.filter_config_fingerprint or fingerprint_for_snapshot(snapshot)
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
                evaluation_subject="simulation" if params.filter_config_snapshot_json else "production",
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
                self._record_assessment(
                    assessments,
                    self._failed_assessment(params.run_id, item, "missing_production_result"),
                )
            elif item.simulation_result is None:
                self._record_assessment(
                    assessments,
                    self._failed_assessment(params.run_id, item, "missing_simulation_result"),
                )

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

        consecutive_classification_failures = 0
        comparison_by_id = {item.article.id: item for item in comparisons}
        tasks = [
            _EvaluationTask(item=item, scope=EvaluationScope.REJECTED_POPULATION)
            for item in rejected
        ] + [
            _EvaluationTask(item=item, scope=EvaluationScope.ACCEPTED_AUDIT)
            for item in accepted_sample
        ]
        for assessment in self._classify_tasks_in_order(
            run_id=params.run_id,
            tasks=tasks,
            filter_config_snapshot_json=filter_config_snapshot_json,
            comparison_by_id=comparison_by_id,
        ):
            assessment = self._record_assessment(
                assessments,
                assessment,
            )
            consecutive_classification_failures = _next_consecutive_classification_failures(
                assessment,
                consecutive_classification_failures,
            )
            _raise_if_repeated_classification_failures(consecutive_classification_failures)
        return assessments

    def _classify_tasks_in_order(
        self,
        *,
        run_id: str,
        tasks: list[_EvaluationTask],
        filter_config_snapshot_json: dict[str, Any],
        comparison_by_id: dict[str, ComparisonItem],
    ) -> Iterator[ItemAssessment]:
        concurrency = max(1, int(getattr(self._settings, "classification_concurrency", 1)))
        if concurrency == 1:
            for task in tasks:
                yield self._classify_or_fail(
                    run_id,
                    task.item,
                    task.scope,
                    filter_config_snapshot_json,
                    comparison_by_id,
                )
            return

        futures: dict[int, Future[ItemAssessment]] = {}
        next_to_submit = 0
        next_to_commit = 0

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="filter-quality-item") as executor:
            try:
                while next_to_commit < len(tasks):
                    while next_to_submit < len(tasks) and len(futures) < concurrency:
                        task = tasks[next_to_submit]
                        futures[next_to_submit] = executor.submit(
                            self._classify_or_fail,
                            run_id,
                            task.item,
                            task.scope,
                            filter_config_snapshot_json,
                            comparison_by_id,
                        )
                        next_to_submit += 1

                    future = futures.pop(next_to_commit)
                    yield future.result()
                    next_to_commit += 1
            except BaseException:
                for future in futures.values():
                    future.cancel()
                raise

    def _record_assessment(
        self,
        assessments: list[ItemAssessment],
        assessment: ItemAssessment,
    ) -> ItemAssessment:
        assessments.append(assessment)
        self._repository.insert_item_assessment(assessment)
        return assessment

    def _classify_or_fail(
        self,
        run_id: str,
        item: ComparisonItem,
        scope: EvaluationScope,
        filter_config_snapshot_json: dict[str, Any],
        comparison_by_id: dict[str, ComparisonItem],
    ) -> ItemAssessment:
        try:
            deterministic = _deterministic_duplicate_result(
                item,
                scope=scope,
                filter_config_snapshot_json=filter_config_snapshot_json,
                comparison_by_id=comparison_by_id,
            )
            if deterministic is not None:
                return self._evaluated_assessment(run_id, item, scope, deterministic)
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
                _error_details(error),
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


def _next_consecutive_classification_failures(
    assessment: ItemAssessment,
    current_count: int,
) -> int:
    if (
        assessment.item_status == ItemStatus.FAILED
        and assessment.item_error_code == "llm_classification_failed"
    ):
        return current_count + 1
    return 0


def _raise_if_repeated_classification_failures(count: int) -> None:
    if count >= _MAX_CONSECUTIVE_CLASSIFICATION_FAILURES:
        raise RepeatedLlmClassificationFailure(failure_count=count)


def _deterministic_duplicate_result(
    item: ComparisonItem,
    *,
    scope: EvaluationScope,
    filter_config_snapshot_json: dict[str, Any],
    comparison_by_id: dict[str, ComparisonItem],
) -> ClassificationResult | None:
    if scope != EvaluationScope.REJECTED_POPULATION or item.simulation_result is None:
        return None
    reason = item.simulation_result.rejection_reason_code
    if reason not in {"rejected_soft_duplicate", "rejected_strong_duplicate"}:
        return None

    matched_id = item.simulation_result.matched_article_id or str(
        item.simulation_result.details.get("matched_event_id") or ""
    )
    matched = comparison_by_id.get(matched_id)
    if matched is None or matched.simulation_result is None:
        return _duplicate_classification(
            ClassificationLabel.INCORRECTLY_REJECTED,
            ProbableCause.DEDUPE_THRESHOLD_ISSUE,
            "The duplicate target is not present in the evaluated dataset, so the rejected article may be the only available copy.",
            "Review dedupe result persistence for this article pair before changing include keywords.",
        )
    if matched.simulation_result.outcome.value != "accepted":
        return _duplicate_classification(
            ClassificationLabel.INCORRECTLY_REJECTED,
            ProbableCause.DEDUPE_THRESHOLD_ISSUE,
            "The duplicate target was not accepted, so dedupe suppressed this article without an accepted replacement.",
            "Ensure duplicate suppression only targets articles that already have an accepted equivalent.",
        )

    new_watched = _new_watched_entities(
        item.article,
        matched.article,
        filter_config_snapshot_json=filter_config_snapshot_json,
    )
    if new_watched:
        formatted = ", ".join(sorted(new_watched))
        return _duplicate_classification(
            ClassificationLabel.INCORRECTLY_REJECTED,
            ProbableCause.WATCHLIST_COVERAGE_GAP,
            f"The rejected article introduced watched entity or ticker {formatted} that was absent from the accepted duplicate target.",
            "Review entity extraction and dedupe handling for same-story updates that introduce watched securities.",
        )

    if _same_story_duplicate(item, matched):
        return _duplicate_classification(
            ClassificationLabel.CORRECTLY_REJECTED,
            ProbableCause.LOW_VALUE_NOISE,
            "The rejected article matched an already accepted article with the same story context, so accepting both would add little new signal.",
            "No include-keyword change is needed; keep dedupe behavior for this duplicate pair.",
        )

    return None


def _duplicate_classification(
    label: ClassificationLabel,
    probable_cause: ProbableCause,
    rationale: str,
    improvement_suggestion: str,
) -> ClassificationResult:
    return ClassificationResult(
        classification_label=label,
        classification_confidence=Decimal("1.00"),
        rationale=rationale,
        probable_cause=probable_cause,
        improvement_suggestion=improvement_suggestion,
        suggestion_json={"recommended_include_keywords": []},
        llm_model="deterministic_duplicate_rule",
    )


def _new_watched_entities(
    article,
    matched_article,
    *,
    filter_config_snapshot_json: dict[str, Any],
) -> set[str]:
    watched = {
        str(value).strip().upper()
        for value in filter_config_snapshot_json.get("watchlist_tickers", [])
        if str(value).strip()
    }
    if not watched:
        return set()
    article_entities = _article_watched_entities(article, watched)
    matched_entities = _article_watched_entities(matched_article, watched)
    return article_entities - matched_entities


def _article_watched_entities(article, watched: set[str]) -> set[str]:
    ticker_entities = {str(value).strip().upper() for value in article.tickers if str(value).strip()}
    text = f"{article.headline}\n{article.summary or ''}".upper()
    text_entities = {
        ticker
        for ticker in watched
        if re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", text)
    }
    return (ticker_entities | text_entities) & watched


def _same_story_duplicate(item: ComparisonItem, matched: ComparisonItem) -> bool:
    if (
        normalize_canonical_locator(item.article.url)
        and normalize_canonical_locator(item.article.url) == normalize_canonical_locator(matched.article.url)
    ):
        return True
    result = item.simulation_result
    if result is None:
        return False
    threshold = _optional_float(result.details.get("threshold"))
    score = result.similarity_score if result.similarity_score is not None else _optional_float(result.details.get("score"))
    if threshold is not None and score is not None and score >= threshold:
        return True
    return False


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_details(error: Exception) -> dict[str, Any]:
    message = str(error)
    details: dict[str, Any] = {"exception_type": error.__class__.__name__}
    if message:
        details["message"] = message[:1000]
    return details
