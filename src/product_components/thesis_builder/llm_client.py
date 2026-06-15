from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from .models import LlmAnalysisResult, ThesisStrategy, TradeDirection


class TokenBudgetExhausted(RuntimeError):
    """Raised when the daily/run token ceiling would be exceeded."""


class ThesisLlmClient(Protocol):
    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        """Return parsed structured LLM JSON."""


@dataclass
class OpenAIThesisClient:
    """Thin OpenAI Responses API adapter for ThesisBuilder."""

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        from openai import OpenAI

        client = OpenAI()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _THESIS_ANALYSIS_RESPONSE_FORMAT},
            temperature=0,
            store=False,
        )
        return _load_json_object(getattr(response, "output_text", ""))


@dataclass
class ThesisAnalyzer:
    client: ThesisLlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    tokens_used: int = 0

    def __post_init__(self) -> None:
        self._budget_lock = threading.Lock()

    def analyze_article(
        self,
        *,
        article,
        ticker: str,
        exchange_code: str,
        market_context_snapshot: dict[str, Any] | None = None,
    ) -> LlmAnalysisResult:
        prompt = _build_prompt(
            article=article,
            ticker=ticker,
            exchange_code=exchange_code,
            market_context_snapshot=market_context_snapshot,
        )
        estimated_tokens = _estimate_tokens(prompt) + self.max_tokens_per_item
        self._reserve_tokens(estimated_tokens)
        try:
            raw = self.client.analyze(
                model=self.model,
                prompt=prompt,
                max_output_tokens=self.max_tokens_per_item,
            )
        except Exception:
            self._release_reserved_tokens(estimated_tokens)
            raise
        result = parse_analysis_result(raw, expected_ticker=ticker, expected_exchange_code=exchange_code)
        actual_tokens = int(raw.get("estimated_tokens") or estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        return LlmAnalysisResult(
            **{**result.__dict__, "estimated_tokens": actual_tokens, "llm_model": self.model}
        )

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


def parse_analysis_result(
    raw: dict[str, Any],
    *,
    expected_ticker: str,
    expected_exchange_code: str,
) -> LlmAnalysisResult:
    ticker = str(raw.get("ticker") or "").strip().upper()
    exchange_code = str(raw.get("exchange_code") or "").strip().upper()
    if ticker != expected_ticker.strip().upper() or exchange_code != expected_exchange_code.strip().upper():
        raise ValueError("instrument_mismatch")

    sentiment = _float_in_range(raw.get("sentiment"), minimum=-1.0, maximum=1.0, field="sentiment")
    relevance = _float_in_range(raw.get("relevance"), minimum=0.0, maximum=1.0, field="relevance")
    confidence = _float_in_range(raw.get("confidence"), minimum=0.0, maximum=1.0, field="confidence")
    strategy = ThesisStrategy(str(raw.get("candidate_strategy")))
    direction = TradeDirection(str(raw.get("direction")))
    bullets = raw.get("evidence_bullet_candidates")
    if not isinstance(bullets, list):
        bullets = []
    return LlmAnalysisResult(
        ticker=ticker,
        exchange_code=exchange_code,
        sentiment=sentiment,
        relevance=relevance,
        urgency=str(raw.get("urgency") or "informational"),
        suggested_action=str(raw.get("suggested_action") or "hold"),
        candidate_strategy=strategy,
        direction=direction,
        confidence=confidence,
        reasoning=str(raw.get("reasoning") or ""),
        is_market_moving=bool(raw.get("is_market_moving", direction is not TradeDirection.HOLD)),
        event_type=str(raw["event_type"]) if raw.get("event_type") else None,
        price_impact_magnitude=(
            str(raw["price_impact_magnitude"]) if raw.get("price_impact_magnitude") else None
        ),
        evidence_bullet_candidates=[str(item).strip() for item in bullets if str(item).strip()],
    )


def _build_prompt(*, article, ticker: str, exchange_code: str, market_context_snapshot: dict[str, Any] | None) -> str:
    return json.dumps(
        {
            "task": "Analyze whether this accepted financial news article supports a thesis card.",
            "strategy_scope_v1": ["event_driven", "sentiment_momentum"],
            "unsupported_strategies_must_still_be_labeled_if_best_fit": [
                "sector_rotation",
                "contrarian_reversal",
                "trend_follow",
            ],
            "instrument": {"ticker": ticker, "exchange_code": exchange_code},
            "article": {
                "id": article.id,
                "source": article.source,
                "headline": article.headline,
                "summary": article.summary,
                "url": article.url,
                "tickers": article.tickers,
                "published_at": article.published_at.isoformat(),
                "sentiment_source": article.sentiment_source,
            },
            "market_context": market_context_snapshot,
            "required_json_fields": [
                "ticker",
                "exchange_code",
                "sentiment",
                "relevance",
                "urgency",
                "suggested_action",
                "candidate_strategy",
                "direction",
                "confidence",
                "reasoning",
                "is_market_moving",
                "evidence_bullet_candidates",
                "estimated_tokens",
            ],
        },
        sort_keys=True,
    )


def _float_in_range(value: Any, *, minimum: float, maximum: float, field: str) -> float:
    parsed = float(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"invalid_{field}")
    return parsed


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _load_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("empty thesis response", text, 0)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("thesis_response_not_object")
    return payload


_THESIS_ANALYSIS_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "thesis_builder_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ticker",
            "exchange_code",
            "sentiment",
            "relevance",
            "urgency",
            "suggested_action",
            "candidate_strategy",
            "direction",
            "confidence",
            "reasoning",
            "is_market_moving",
            "event_type",
            "price_impact_magnitude",
            "evidence_bullet_candidates",
            "estimated_tokens",
        ],
        "properties": {
            "ticker": {"type": "string"},
            "exchange_code": {"type": "string"},
            "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
            "relevance": {"type": "number", "minimum": 0, "maximum": 1},
            "urgency": {"type": "string"},
            "suggested_action": {"type": "string"},
            "candidate_strategy": {
                "type": "string",
                "enum": [
                    "event_driven",
                    "sentiment_momentum",
                    "sector_rotation",
                    "contrarian_reversal",
                    "trend_follow",
                ],
            },
            "direction": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string"},
            "is_market_moving": {"type": "boolean"},
            "event_type": {"type": ["string", "null"]},
            "price_impact_magnitude": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
            "evidence_bullet_candidates": {"type": "array", "items": {"type": "string"}},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}
