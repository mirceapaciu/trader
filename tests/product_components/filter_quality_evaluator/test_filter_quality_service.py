from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.core_components.event_ingestion_engine.models import FilterOutcome, FilterResult
from src.product_components.filter_quality_evaluator.models import (
    ComparisonItem,
    InputArticle,
)
from src.product_components.filter_quality_evaluator.service import (
    FilterQualityEvaluatorService,
    RepeatedLlmClassificationFailure,
)


class FailingClassifier:
    def classify_item(self, **_kwargs):
        raise json.JSONDecodeError("invalid json", "", 0)


class RecordingRepository:
    def __init__(self) -> None:
        self.assessments = []

    def insert_item_assessment(self, assessment) -> None:
        self.assessments.append(assessment)


def test_evaluate_items_fails_fast_after_repeated_llm_classification_failures() -> None:
    repository = RecordingRepository()
    service = FilterQualityEvaluatorService(
        settings=SimpleNamespace(accepted_audit_sample_size=200),
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
