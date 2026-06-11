from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
        self.prompt = None

    def classify(self, *, model: str, prompt: str, max_output_tokens: int):
        self.prompt = prompt
        return dict(self.payload)


class BlockingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.release = threading.Event()
        self._lock = threading.Lock()

    def classify(self, *, model: str, prompt: str, max_output_tokens: int):
        with self._lock:
            self.calls += 1
        self.release.wait(timeout=2)
        return {
            "classification_label": "incorrectly_rejected",
            "classification_confidence": 0.75,
            "rationale": "Relevant to watched ticker.",
            "probable_cause": "keyword_gap",
            "improvement_suggestion": "Add guidance keyword.",
            "suggestion_json": {"recommended_include_keywords": ["guidance"]},
            "estimated_tokens": 10,
        }


def _item() -> ComparisonItem:
    return _item_with_text(
        headline="Apple raises guidance",
        summary="Revenue outlook improved",
        tickers=["AAPL"],
    )


def _item_with_text(*, headline: str, summary: str | None, tickers: list[str] | None = None) -> ComparisonItem:
    article = InputArticle(
        id="a1",
        source="finnhub",
        headline=headline,
        summary=summary,
        url="https://example.com/a1",
        tickers=tickers or [],
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
    client = FakeClient(
        {
            "classification_label": "incorrectly_rejected",
            "classification_confidence": 0.75,
            "rationale": "Relevant to watched ticker.",
            "probable_cause": "keyword_gap",
            "improvement_suggestion": "Add guidance keyword.",
            "suggestion_json": {"recommended_include_keywords": ["guidance"]},
            "estimated_tokens": 10,
        }
    )
    classifier = LlmClassifier(
        client=client,
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
    assert result.suggestion_json["recommended_include_keywords"] == ["guidance"]

    prompt = json.loads(client.prompt or "{}")
    relevance_standard = prompt["relevance_standard"]
    assert "stock-market trading" in prompt["task"]
    assert "Company-specific catalysts" in relevance_standard["accept_if"][0]
    assert "Personal finance" in relevance_standard["reject_if"][0]
    assert "retirement planning" in relevance_standard["keyword_recommendation_guidance"]
    assert "exact phrase appears" in relevance_standard["keyword_recommendation_guidance"]


def test_llm_classifier_keeps_only_recommended_keywords_present_in_article_text() -> None:
    classifier = LlmClassifier(
        client=FakeClient(
            {
                "classification_label": "incorrectly_rejected",
                "classification_confidence": 0.75,
                "rationale": "Market-wide catalyst.",
                "probable_cause": "keyword_gap",
                "improvement_suggestion": "Add market phrase.",
                "suggestion_json": {
                    "recommended_include_keywords": [
                        "investor sentiment",
                        "leveraged ETFs",
                        "stock-market bull",
                        "leveraged etfs",
                    ]
                },
                "estimated_tokens": 10,
            }
        ),
        model="test-model",
        max_tokens_per_run=1000,
        max_tokens_per_item=100,
        min_confidence_threshold=0.6,
    )

    result = classifier.classify_item(
        item=_item_with_text(
            headline="How exploding investor euphoria and leveraged ETFs turned one stock-market bull cautious",
            summary="A Barclays strategist explains why it is time to turn cautious on U.S. stocks.",
        ),
        scope=EvaluationScope.REJECTED_POPULATION,
        filter_config_snapshot_json={"include_keywords": []},
    )

    assert result.suggestion_json["recommended_include_keywords"] == ["leveraged etfs", "stock-market bull"]


def test_llm_classifier_rejects_non_contiguous_keyword_recommendations() -> None:
    classifier = LlmClassifier(
        client=FakeClient(
            {
                "classification_label": "incorrectly_rejected",
                "classification_confidence": 0.75,
                "rationale": "Market-wide catalyst.",
                "probable_cause": "keyword_gap",
                "improvement_suggestion": "Add exact phrase.",
                "suggestion_json": {"recommended_include_keywords": ["apple outlook", "revenue outlook"]},
                "estimated_tokens": 10,
            }
        ),
        model="test-model",
        max_tokens_per_run=1000,
        max_tokens_per_item=100,
        min_confidence_threshold=0.6,
    )

    result = classifier.classify_item(
        item=_item_with_text(headline="Apple raises guidance", summary="Revenue outlook improved"),
        scope=EvaluationScope.REJECTED_POPULATION,
        filter_config_snapshot_json={"include_keywords": []},
    )

    assert result.suggestion_json["recommended_include_keywords"] == ["revenue outlook"]


def test_llm_classifier_drops_recommendations_covered_by_existing_include_keywords() -> None:
    classifier = LlmClassifier(
        client=FakeClient(
            {
                "classification_label": "incorrectly_rejected",
                "classification_confidence": 0.75,
                "rationale": "Market-wide catalyst.",
                "probable_cause": "keyword_gap",
                "improvement_suggestion": "Avoid redundant phrase.",
                "suggestion_json": {"recommended_include_keywords": ["investor sentiment", "leveraged ETFs"]},
                "estimated_tokens": 10,
            }
        ),
        model="test-model",
        max_tokens_per_run=1000,
        max_tokens_per_item=100,
        min_confidence_threshold=0.6,
    )

    result = classifier.classify_item(
        item=_item_with_text(
            headline="Investor sentiment rises as leveraged ETFs attract inflows",
            summary=None,
        ),
        scope=EvaluationScope.REJECTED_POPULATION,
        filter_config_snapshot_json={"include_keywords": ["sentiment"]},
    )

    assert result.suggestion_json["recommended_include_keywords"] == ["leveraged etfs"]


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


def test_llm_classifier_reserves_token_budget_across_concurrent_calls() -> None:
    client = BlockingClient()
    classifier = LlmClassifier(
        client=client,
        model="test-model",
        max_tokens_per_run=100,
        max_tokens_per_item=80,
        min_confidence_threshold=0.6,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            classifier.classify_item,
            item=_item(),
            scope=EvaluationScope.REJECTED_POPULATION,
            filter_config_snapshot_json={},
        )
        deadline = time.monotonic() + 2
        while client.calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        second = executor.submit(
            classifier.classify_item,
            item=_item(),
            scope=EvaluationScope.REJECTED_POPULATION,
            filter_config_snapshot_json={},
        )
        with pytest.raises(TokenBudgetExhausted):
            second.result(timeout=2)
        client.release.set()
        first.result(timeout=2)

    assert client.calls == 1


def test_load_json_object_accepts_wrapped_json_object() -> None:
    payload = _load_json_object('```json\n{"classification_label": "correctly_rejected"}\n```')

    assert payload == {"classification_label": "correctly_rejected"}
