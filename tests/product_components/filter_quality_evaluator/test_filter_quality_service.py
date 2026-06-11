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
    article = InputArticle(
        id=article_id,
        source="rss",
        headline=f"Headline {index}",
        summary="Summary",
        url=f"https://example.com/{index}",
        tickers=[],
        published_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        sentiment_source=None,
    )
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
