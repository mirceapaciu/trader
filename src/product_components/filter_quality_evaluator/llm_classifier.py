from __future__ import annotations

import json
import threading
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
from .keyword_recommendations import sanitize_suggestion_json


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
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _CLASSIFIER_RESPONSE_FORMAT},
            temperature=0,
            store=False,
        )
        text = getattr(response, "output_text", "")
        return _load_json_object(text)


@dataclass
class LlmClassifier:
    client: LlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    min_confidence_threshold: float
    tokens_used: int = 0

    def __post_init__(self) -> None:
        self._budget_lock = threading.Lock()

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
        self._reserve_tokens(estimated_prompt_tokens)

        try:
            raw = self.client.classify(
                model=self.model,
                prompt=_build_prompt(
                    item=item,
                    scope=scope,
                    filter_config_snapshot_json=filter_config_snapshot_json,
                ),
                max_output_tokens=self.max_tokens_per_item,
            )
        except Exception:
            self._release_reserved_tokens(estimated_prompt_tokens)
            raise
        result = _parse_result(raw, scope=scope, model=self.model)
        result = _sanitize_result_recommendations(
            result,
            item=item,
            filter_config_snapshot_json=filter_config_snapshot_json,
        )
        self._settle_tokens(
            reserved_tokens=estimated_prompt_tokens,
            actual_tokens=int(raw.get("estimated_tokens") or estimated_prompt_tokens),
        )
        return result

    def _reserve_tokens(self, estimated_tokens: int) -> None:
        with self._budget_lock:
            if self.tokens_used + estimated_tokens > self.max_tokens_per_run:
                raise TokenBudgetExhausted("token_budget_exhausted")
            self.tokens_used += estimated_tokens

    def _release_reserved_tokens(self, reserved_tokens: int) -> None:
        with self._budget_lock:
            self.tokens_used -= reserved_tokens

    def _settle_tokens(self, *, reserved_tokens: int, actual_tokens: int) -> None:
        with self._budget_lock:
            self.tokens_used += actual_tokens - reserved_tokens


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
            "task": (
                "Evaluate whether the news filter made the right decision for stock-market trading. "
                "The target accepted set is news that could plausibly impact stock prices or market "
                "prices for watched securities."
            ),
            "relevance_standard": {
                "accept_if": [
                    "Company-specific catalysts such as earnings, guidance, revenue, margins, orders, contracts, product launches, leadership changes, layoffs, buybacks, dividends, debt, liquidity, bankruptcy, M&A, IPOs, spinoffs, litigation, investigations, regulatory approvals, regulatory actions, analyst actions, ratings changes, price target changes, insider or institutional activity.",
                    "Sector, commodity, rate, currency, credit, inflation, jobs, GDP, central-bank, geopolitical, supply-chain, or policy news likely to move broad markets, sectors, or watched tickers.",
                    "News that names or clearly implicates a tradable company, ticker, sector, commodity, or macro driver and provides a plausible near- or medium-term price catalyst.",
                ],
                "reject_if": [
                    "Personal finance, retirement planning, wealth management, tax advice, budgeting, real estate advice, relationship money advice, generic investing education, evergreen explainers, opinion without a tradable catalyst, or consumer advice.",
                    "Articles that mention money, investing, or retirement but do not identify a market-moving event, tradable asset, ticker, sector, or macro catalyst.",
                    "Low-value duplicates, stale summaries, advertisements, newsletters, or broad commentary with no actionable stock-price impact.",
                ],
                "label_guidance": {
                    "correctly_rejected": "Use when a rejected article lacks a plausible stock-price or market-price catalyst.",
                    "incorrectly_rejected": "Use only when a rejected article contains a plausible stock-price or market-price catalyst and should have passed the filter.",
                    "correctly_accepted": "Use when an accepted article contains a plausible stock-price or market-price catalyst.",
                    "incorrectly_accepted": "Use when an accepted article is only general finance, personal finance, evergreen education, opinion, or other non-catalyst content.",
                },
                "keyword_recommendation_guidance": (
                    "Recommend include keywords only for terms that would improve detection of stock-price-impacting "
                    "news and only if the exact phrase appears in the provided article headline or summary. "
                    "Copy the phrase exactly from the article text except for casing. Do not recommend inferred "
                    "concepts, paraphrases, synonyms, or umbrella topics that are not present in the text. Do not "
                    "recommend broad personal-finance terms such as personal finance, retirement "
                    "planning, wealth management, financial compatibility, budgeting, or relationship money advice "
                    "unless the article also contains a concrete market-moving catalyst. Do not recommend a phrase "
                    "that is already covered by an existing include keyword in filter_config_snapshot_json."
                ),
            },
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


def _sanitize_result_recommendations(
    result: ClassificationResult,
    *,
    item: ComparisonItem,
    filter_config_snapshot_json: dict[str, Any],
) -> ClassificationResult:
    suggestion_json = sanitize_suggestion_json(
        result.suggestion_json,
        headline=item.article.headline,
        summary=item.article.summary,
        existing_include_keywords=_string_list(filter_config_snapshot_json.get("include_keywords")),
    )
    return ClassificationResult(
        classification_label=result.classification_label,
        classification_confidence=result.classification_confidence,
        rationale=result.rationale,
        probable_cause=result.probable_cause,
        improvement_suggestion=result.improvement_suggestion,
        suggestion_json=suggestion_json,
        llm_model=result.llm_model,
        estimated_tokens=result.estimated_tokens,
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _load_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("empty classifier response", text, 0)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("classifier_response_not_object")
    return payload


_CLASSIFIER_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "filter_quality_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "classification_label",
            "classification_confidence",
            "rationale",
            "probable_cause",
            "improvement_suggestion",
            "suggestion_json",
            "estimated_tokens",
        ],
        "properties": {
            "classification_label": {
                "type": "string",
                "enum": [
                    "correctly_rejected",
                    "incorrectly_rejected",
                    "correctly_accepted",
                    "incorrectly_accepted",
                ],
            },
            "classification_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "rationale": {"type": "string"},
            "probable_cause": {
                "type": "string",
                "enum": [
                    "keyword_gap",
                    "watchlist_coverage_gap",
                    "dedupe_threshold_issue",
                    "rule_conflict",
                    "low_value_noise",
                ],
            },
            "improvement_suggestion": {"type": "string"},
            "suggestion_json": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "recommended_include_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["recommended_include_keywords"],
            },
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}
