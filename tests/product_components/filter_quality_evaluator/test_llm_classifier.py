from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core_components.event_ingestion_engine.models import FilterOutcome, FilterResult
from src.product_components.filter_quality_evaluator.llm_classifier import (
    LlmClassifier,
    TokenBudgetExhausted,
    _load_json_object,
)
from src.product_components.filter_quality_evaluator.models import ComparisonItem, EvaluationScope, InputArticle


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def classify(self, *, model: str, prompt: str, max_output_tokens: int):
        return dict(self.payload)


def _item() -> ComparisonItem:
    article = InputArticle(
        id="a1",
        source="finnhub",
        headline="Apple raises guidance",
        summary="Revenue outlook improved",
        url="https://example.com/a1",
        tickers=["AAPL"],
        published_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        sentiment_source=None,
    )
    return ComparisonItem(
        article=article,
        filter_run_id_production="prod",
        filter_run_id_simulation="sim",
        production_result=FilterResult(article_id="a1", outcome=FilterOutcome.REJECTED),
        simulation_result=FilterResult(article_id="a1", outcome=FilterOutcome.REJECTED),
    )


def test_llm_classifier_validates_structured_response() -> None:
    classifier = LlmClassifier(
        client=FakeClient(
            {
                "classification_label": "incorrectly_rejected",
                "classification_confidence": 0.75,
                "rationale": "Relevant to watched ticker.",
                "probable_cause": "keyword_gap",
                "improvement_suggestion": "Add guidance keyword.",
                "suggestion_json": {"keyword": "guidance"},
                "estimated_tokens": 10,
            }
        ),
        model="test-model",
        max_tokens_per_run=1000,
        max_tokens_per_item=100,
        min_confidence_threshold=0.6,
    )

    result = classifier.classify_item(
        item=_item(),
        scope=EvaluationScope.REJECTED_POPULATION,
        filter_config_snapshot_json={},
    )

    assert result.classification_label.value == "incorrectly_rejected"
    assert result.llm_model == "test-model"


def test_llm_classifier_fails_closed_on_budget() -> None:
    classifier = LlmClassifier(
        client=FakeClient({}),
        model="test-model",
        max_tokens_per_run=1,
        max_tokens_per_item=100,
        min_confidence_threshold=0.6,
    )

    with pytest.raises(TokenBudgetExhausted):
        classifier.classify_item(
            item=_item(),
            scope=EvaluationScope.REJECTED_POPULATION,
            filter_config_snapshot_json={},
        )


def test_load_json_object_accepts_wrapped_json_object() -> None:
    payload = _load_json_object('```json\n{"classification_label": "correctly_rejected"}\n```')

    assert payload == {"classification_label": "correctly_rejected"}
