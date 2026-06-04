from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .models import (
    ClassificationLabel,
    ClassificationResult,
    ComparisonItem,
    EvaluationScope,
    ProbableCause,
)


class TokenBudgetExhausted(RuntimeError):
    """Raised when the run-level token ceiling would be exceeded."""


class LlmClient(Protocol):
    def classify(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        """Return parsed classifier JSON."""


@dataclass
class OpenAIResponsesClient:
    """Thin OpenAI Responses API adapter."""

    def classify(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        from openai import OpenAI

        client = OpenAI()
        response = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=max_output_tokens,
        )
        text = getattr(response, "output_text", "")
        return json.loads(text)


@dataclass
class LlmClassifier:
    client: LlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    min_confidence_threshold: float
    tokens_used: int = 0

    def classify_item(
        self,
        *,
        item: ComparisonItem,
        scope: EvaluationScope,
        filter_config_snapshot_json: dict[str, Any],
    ) -> ClassificationResult:
        estimated_prompt_tokens = _estimate_tokens(
            item.article.headline,
            item.article.summary or "",
            json.dumps(filter_config_snapshot_json, sort_keys=True),
        ) + self.max_tokens_per_item
        if self.tokens_used + estimated_prompt_tokens > self.max_tokens_per_run:
            raise TokenBudgetExhausted("token_budget_exhausted")

        raw = self.client.classify(
            model=self.model,
            prompt=_build_prompt(
                item=item,
                scope=scope,
                filter_config_snapshot_json=filter_config_snapshot_json,
            ),
            max_output_tokens=self.max_tokens_per_item,
        )
        result = _parse_result(raw, scope=scope, model=self.model)
        self.tokens_used += int(raw.get("estimated_tokens") or estimated_prompt_tokens)
        return result


def _build_prompt(
    *,
    item: ComparisonItem,
    scope: EvaluationScope,
    filter_config_snapshot_json: dict[str, Any],
) -> str:
    article = item.article
    simulation = item.simulation_result
    production = item.production_result
    return json.dumps(
        {
            "task": "Evaluate financial news filtering quality.",
            "required_json_fields": [
                "classification_label",
                "classification_confidence",
                "rationale",
                "probable_cause",
                "improvement_suggestion",
                "suggestion_json",
            ],
            "scope": scope.value,
            "article": {
                "headline": article.headline,
                "summary": article.summary,
                "source": article.source,
                "occurred_at": article.published_at.isoformat(),
                "entities": article.tickers,
                "attributes": {
                    "sentiment_source": article.sentiment_source,
                },
            },
            "production_filter_outcome": production.outcome.value if production else None,
            "simulation_filter_outcome": simulation.outcome.value if simulation else None,
            "rejection_reason_code": simulation.rejection_reason_code if simulation else None,
            "filter_config_snapshot_json": filter_config_snapshot_json,
            "label_rules": {
                "rejected_population": ["correctly_rejected", "incorrectly_rejected"],
                "accepted_audit": ["correctly_accepted", "incorrectly_accepted"],
            },
        },
        sort_keys=True,
    )


def _parse_result(raw: dict[str, Any], *, scope: EvaluationScope, model: str) -> ClassificationResult:
    label = ClassificationLabel(str(raw.get("classification_label")))
    if scope == EvaluationScope.REJECTED_POPULATION and label not in {
        ClassificationLabel.CORRECTLY_REJECTED,
        ClassificationLabel.INCORRECTLY_REJECTED,
    }:
        raise ValueError("invalid_rejected_scope_label")
    if scope == EvaluationScope.ACCEPTED_AUDIT and label not in {
        ClassificationLabel.CORRECTLY_ACCEPTED,
        ClassificationLabel.INCORRECTLY_ACCEPTED,
    }:
        raise ValueError("invalid_accepted_scope_label")

    confidence = Decimal(str(raw.get("classification_confidence")))
    if confidence < 0 or confidence > 1:
        raise ValueError("invalid_classification_confidence")

    probable_cause = ProbableCause(str(raw.get("probable_cause")))
    suggestion_json = raw.get("suggestion_json")
    if not isinstance(suggestion_json, dict):
        suggestion_json = {}

    return ClassificationResult(
        classification_label=label,
        classification_confidence=confidence,
        rationale=str(raw.get("rationale") or ""),
        probable_cause=probable_cause,
        improvement_suggestion=str(raw.get("improvement_suggestion") or ""),
        suggestion_json=suggestion_json,
        llm_model=model,
        estimated_tokens=int(raw.get("estimated_tokens") or 0),
    )


def _estimate_tokens(*parts: str) -> int:
    # Conservative local estimate for budget gating before the provider returns usage.
    return max(1, sum(len(part) for part in parts) // 4)
