from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core_components.event_ingestion_engine.models import FilterOutcome, FilterResult
from src.product_components.filter_quality_evaluator.models import (
    ClassificationLabel,
    ClassificationResult,
    ComparisonItem,
    EvaluationScope,
    FilterQualityRunParams,
    InputArticle,
    ProbableCause,
)
from src.product_components.filter_quality_evaluator.service import (
    FilterQualityEvaluatorService,
    RepeatedLlmClassificationFailure,
)


class FailingClassifier:
    def classify_item(self, **_kwargs):
        raise json.JSONDecodeError("invalid json", "", 0)


class SlowRecordingClassifier:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}
        self.active_count = 0
        self.max_active_count = 0
        self._lock = threading.Lock()

    def classify_item(self, *, item, scope, **_kwargs):
        with self._lock:
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
        try:
            time.sleep(self.delays.get(item.article.id, 0.02))
            label = (
                ClassificationLabel.CORRECTLY_ACCEPTED
                if scope == EvaluationScope.ACCEPTED_AUDIT
                else ClassificationLabel.CORRECTLY_REJECTED
            )
            return ClassificationResult(
                classification_label=label,
                classification_confidence=Decimal("0.90"),
                rationale=f"Rationale for {item.article.id}",
                probable_cause=ProbableCause.LOW_VALUE_NOISE,
                improvement_suggestion="No change.",
                suggestion_json={"recommended_include_keywords": []},
                llm_model="test-model",
            )
        finally:
            with self._lock:
                self.active_count -= 1


class RecordingRepository:
    def __init__(self) -> None:
        self.assessments = []

    def insert_item_assessment(self, assessment) -> None:
        self.assessments.append(assessment)


class ReplayRepository:
    def __init__(
        self,
        *,
        articles: list[InputArticle],
        production_snapshot: dict,
        production_results: dict[str, FilterResult],
    ) -> None:
        self.articles = articles
        self.production_snapshot = production_snapshot
        self.production_results = production_results
        self.simulation_results: dict[str, FilterResult] = {}
        self.simulation_snapshot: dict | None = None
        self.final_summary = None
        self.failure_code = None
        self.assessments = []

    def load_input_articles(self, **_kwargs) -> list[InputArticle]:
        return self.articles

    def create_run(self, **_kwargs) -> None:
        return None

    def resolve_production_filter_run_id(self, **_kwargs) -> str:
        return "prod_snapshot"

    def load_filter_run_snapshot(self, *, filter_run_id: str) -> dict:
        assert filter_run_id == "prod_snapshot"
        return self.production_snapshot

    def persist_simulation_results(self, *, filter_run, results) -> None:
        self.simulation_snapshot = filter_run.filter_config_snapshot_json
        self.simulation_results = {result.article_id: result for result in results}

    def load_comparison_items(self, *, articles, production_filter_run_id, simulation_filter_run_id):
        return [
            ComparisonItem(
                article=article,
                filter_run_id_production=production_filter_run_id,
                filter_run_id_simulation=simulation_filter_run_id,
                production_result=self.production_results.get(article.id),
                simulation_result=self.simulation_results.get(article.id),
            )
            for article in articles
        ]

    def insert_item_assessment(self, assessment) -> None:
        self.assessments.append(assessment)

    def finalize_run_success(self, *, run_id: str, summary) -> None:
        self.final_summary = summary

    def finalize_run_failure(self, *, run_id: str, error_code: str, error_details_json=None) -> None:
        self.failure_code = error_code


def test_production_run_replays_persisted_production_filter_snapshot_not_env_settings() -> None:
    article = _input_article(
        "a1",
        headline="Micron enters a bearish phase",
        summary="The tech sector correction accelerated.",
        published_at=datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
    )
    repository = ReplayRepository(
        articles=[article],
        production_snapshot=_snapshot(include_keywords=["bearish"]),
        production_results={"a1": FilterResult(article_id="a1", outcome=FilterOutcome.ACCEPTED)},
    )
    service = FilterQualityEvaluatorService(
        settings=_settings(),
        news_settings=SimpleNamespace(include_keywords=(), exclude_keywords=()),
        repository=repository,
        classifier=SlowRecordingClassifier(),
    )

    service.run(_run_params())

    assert repository.failure_code is None
    assert repository.simulation_snapshot is not None
    assert repository.simulation_snapshot["include_keywords"] == ["bearish"]
    assert repository.simulation_results["a1"].outcome == FilterOutcome.ACCEPTED
    assert repository.final_summary.evaluation_subject == "production"


def test_production_run_replays_dedupe_with_persisted_production_filter_snapshot() -> None:
    earlier = _input_article(
        "a1",
        headline="Micron, Intel drag the tech sector into a new bearish phase. Will the correction last this time?",
        summary="The selloff in the tech sector graduated to a new phase: it is now officially a correction.",
        url="https://example.test/marketwatch",
        published_at=datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
    )
    later = _input_article(
        "a2",
        headline="Micron, Marvell drag the tech sector into a new bearish phase. Will the correction last this time?",
        summary="The selloff in the tech sector graduated to a new phase: it is now officially a correction.",
        url="https://example.test/marketwatch",
        published_at=datetime(2026, 6, 4, 11, tzinfo=timezone.utc),
    )
    repository = ReplayRepository(
        articles=[earlier, later],
        production_snapshot=_snapshot(include_keywords=["bearish"]),
        production_results={
            "a1": FilterResult(article_id="a1", outcome=FilterOutcome.ACCEPTED),
            "a2": FilterResult(
                article_id="a2",
                outcome=FilterOutcome.REJECTED,
                rejection_reason_code="rejected_soft_duplicate",
            ),
        },
    )
    service = FilterQualityEvaluatorService(
        settings=_settings(),
        news_settings=SimpleNamespace(include_keywords=(), exclude_keywords=()),
        repository=repository,
        classifier=SlowRecordingClassifier(),
    )

    service.run(_run_params())

    assert repository.failure_code is None
    assert repository.simulation_results["a1"].outcome == FilterOutcome.ACCEPTED
    assert repository.simulation_results["a2"].outcome == FilterOutcome.REJECTED
    assert repository.simulation_results["a2"].rejection_reason_code == "rejected_soft_duplicate"


def test_evaluate_items_fails_fast_after_repeated_llm_classification_failures() -> None:
    repository = RecordingRepository()
    service = FilterQualityEvaluatorService(
        settings=SimpleNamespace(accepted_audit_sample_size=200, classification_concurrency=2),
        news_settings=SimpleNamespace(),
        repository=repository,
        classifier=FailingClassifier(),
    )

    with pytest.raises(RepeatedLlmClassificationFailure):
        service._evaluate_items(
            params=SimpleNamespace(
                run_id="fqe_test",
                accepted_audit_enabled=False,
                accepted_audit_sample_size=None,
            ),
            comparisons=[_comparison(index) for index in range(5)],
            filter_config_snapshot_json={},
        )

    assert len(repository.assessments) == 3
    assert all(assessment.item_error_code == "llm_classification_failed" for assessment in repository.assessments)
    assert all(
        assessment.item_error_details_json["exception_type"] == "JSONDecodeError"
        for assessment in repository.assessments
    )
    assert all("message" in assessment.item_error_details_json for assessment in repository.assessments)


def test_evaluate_items_classifies_articles_in_parallel() -> None:
    classifier = SlowRecordingClassifier()
    service = FilterQualityEvaluatorService(
        settings=SimpleNamespace(accepted_audit_sample_size=200, classification_concurrency=2),
        news_settings=SimpleNamespace(),
        repository=RecordingRepository(),
        classifier=classifier,
    )

    service._evaluate_items(
        params=SimpleNamespace(
            run_id="fqe_test",
            accepted_audit_enabled=False,
            accepted_audit_sample_size=None,
        ),
        comparisons=[_comparison(index) for index in range(4)],
        filter_config_snapshot_json={},
    )

    assert classifier.max_active_count == 2


def test_evaluate_items_inserts_assessments_in_deterministic_order_when_parallel() -> None:
    repository = RecordingRepository()
    service = FilterQualityEvaluatorService(
        settings=SimpleNamespace(accepted_audit_sample_size=200, classification_concurrency=3),
        news_settings=SimpleNamespace(),
        repository=repository,
        classifier=SlowRecordingClassifier({"a0": 0.03, "a1": 0.02, "a2": 0.01}),
    )

    service._evaluate_items(
        params=SimpleNamespace(
            run_id="fqe_test",
            accepted_audit_enabled=False,
            accepted_audit_sample_size=None,
        ),
        comparisons=[_comparison(index) for index in range(3)],
        filter_config_snapshot_json={},
    )

    assert [assessment.article_id for assessment in repository.assessments] == ["a0", "a1", "a2"]


def test_evaluate_items_concurrency_one_stays_sequential() -> None:
    classifier = SlowRecordingClassifier()
    service = FilterQualityEvaluatorService(
        settings=SimpleNamespace(accepted_audit_sample_size=200, classification_concurrency=1),
        news_settings=SimpleNamespace(),
        repository=RecordingRepository(),
        classifier=classifier,
    )

    service._evaluate_items(
        params=SimpleNamespace(
            run_id="fqe_test",
            accepted_audit_enabled=False,
            accepted_audit_sample_size=None,
        ),
        comparisons=[_comparison(index) for index in range(3)],
        filter_config_snapshot_json={},
    )

    assert classifier.max_active_count == 1


def _comparison(index: int) -> ComparisonItem:
    article_id = f"a{index}"
    article = _input_article(article_id, headline=f"Headline {index}", url=f"https://example.com/{index}")
    return ComparisonItem(
        article=article,
        filter_run_id_production="prod",
        filter_run_id_simulation="sim",
        production_result=FilterResult(article_id=article_id, outcome=FilterOutcome.REJECTED),
        simulation_result=FilterResult(
            article_id=article_id,
            outcome=FilterOutcome.REJECTED,
            rejection_reason_code="rejected_not_relevant",
        ),
    )


def _input_article(
    article_id: str,
    *,
    headline: str,
    summary: str | None = "Summary",
    url: str = "https://example.test/article",
    published_at: datetime = datetime(2026, 6, 4, tzinfo=timezone.utc),
) -> InputArticle:
    return InputArticle(
        id=article_id,
        source="rss",
        headline=headline,
        summary=summary,
        url=url,
        tickers=[],
        published_at=published_at,
        fetched_at=published_at,
        sentiment_source=None,
    )


def _snapshot(*, include_keywords: list[str]) -> dict:
    return {
        "include_keywords": include_keywords,
        "exclude_keywords": [],
        "watchlist_tickers": [],
        "dedupe_algorithm": "rapidfuzz_ratio",
        "dedupe_similarity_threshold": 0.9,
        "dedupe_lookback_hours": 24,
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        max_items_per_run=100,
        llm_max_tokens_per_run=1000,
        accepted_audit_sample_size=200,
        classification_concurrency=2,
    )


def _run_params() -> FilterQualityRunParams:
    return FilterQualityRunParams(
        run_id="fqe_test",
        news_window_start_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        news_window_end_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        accepted_audit_enabled=False,
        accepted_audit_sample_size=None,
    )
