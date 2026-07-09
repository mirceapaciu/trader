from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import (
    ContentType,
    LlmAnalysisResult,
    LlmTriageResult,
    ThesisStrategy,
    TradeDirection,
)


class TokenBudgetExhausted(RuntimeError):
    """Raised when the daily/run token ceiling would be exceeded."""


class ThesisLlmClient(Protocol):
    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        """Return parsed structured LLM JSON."""


@dataclass
class OpenAIThesisClient:
    """Thin OpenAI Responses API adapter for ThesisBuilder.

    A per-request ``timeout`` is mandatory: the ThesisBuilder consumer is
    single-threaded, so a stalled HTTP request without a timeout silently
    blocks the entire news loop indefinitely. With a timeout, a stuck request
    raises and the message flows through the normal retry/DLQ path instead.
    """

    api_key: str
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        # Reuse a single client (and its connection pool) across calls instead
        # of constructing a new one per request, which leaks connections.
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                timeout=self.request_timeout_seconds,
                max_retries=self.max_retries,
            )
        return self._client

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        client = self._get_client()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _THESIS_ANALYSIS_RESPONSE_FORMAT},
            temperature=0,
            store=False,
        )
        result = _load_json_object(getattr(response, "output_text", ""))
        # Override LLM's self-reported token count with actual API usage so the
        # budget enforcement in ThesisAnalyzer works correctly.
        usage = getattr(response, "usage", None)
        if usage is not None:
            actual = getattr(usage, "total_tokens", None) or getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)
            if actual:
                result["estimated_tokens"] = int(actual)
        return result

    def analyze_triage(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        client = self._get_client()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _TRIAGE_RESPONSE_FORMAT},
            temperature=0,
            store=False,
        )
        result = _load_json_object(getattr(response, "output_text", ""))
        usage = getattr(response, "usage", None)
        if usage is not None:
            actual = getattr(usage, "total_tokens", None) or getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)
            if actual:
                result["estimated_tokens"] = int(actual)
        return result


@dataclass
class ThesisAnalyzer:
    client: ThesisLlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    triage_model: str | None = None
    triage_max_output_tokens: int = 200
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
        cached = _get_cached_analysis(
            self.client,
            model=self.model,
            prompt=prompt,
            max_output_tokens=self.max_tokens_per_item,
        )
        if cached is not None:
            result = parse_analysis_result(
                cached, expected_ticker=ticker, expected_exchange_code=exchange_code
            )
            actual_tokens = _actual_tokens(cached, fallback_tokens=0)
            return LlmAnalysisResult(
                **{**result.__dict__, "estimated_tokens": actual_tokens, "llm_model": self.model}
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
        actual_tokens = _actual_tokens(raw, fallback_tokens=estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        return LlmAnalysisResult(
            **{**result.__dict__, "estimated_tokens": actual_tokens, "llm_model": self.model}
        )

    def triage_article(
        self,
        *,
        article,
        ticker: str,
        exchange_code: str,
    ) -> LlmTriageResult:
        model = self.triage_model or self.model
        prompt = _build_triage_prompt(article=article, ticker=ticker, exchange_code=exchange_code)
        max_output_tokens = self.triage_max_output_tokens
        estimated_tokens = _estimate_tokens(prompt) + max_output_tokens
        self._reserve_tokens(estimated_tokens)
        try:
            caller = getattr(self.client, "analyze_triage", None)
            if caller is None:
                raw = self.client.analyze(
                    model=model,
                    prompt=prompt,
                    max_output_tokens=max_output_tokens,
                )
            else:
                raw = caller(model=model, prompt=prompt, max_output_tokens=max_output_tokens)
        except Exception:
            self._release_reserved_tokens(estimated_tokens)
            raise
        result = parse_triage_result(
            raw,
            expected_ticker=ticker,
            expected_exchange_code=exchange_code,
        )
        actual_tokens = _actual_tokens(raw, fallback_tokens=estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        return LlmTriageResult(
            **{**result.__dict__, "estimated_tokens": actual_tokens, "llm_model": model}
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
        instrument_is_subject=bool(raw.get("instrument_is_subject", False)),
        content_type=_parse_content_type(raw.get("content_type")),
        event_type=str(raw["event_type"]) if raw.get("event_type") else None,
        price_impact_magnitude=(
            str(raw["price_impact_magnitude"]) if raw.get("price_impact_magnitude") else None
        ),
        evidence_bullet_candidates=[str(item).strip() for item in bullets if str(item).strip()],
    )


def parse_triage_result(
    raw: dict[str, Any],
    *,
    expected_ticker: str,
    expected_exchange_code: str,
) -> LlmTriageResult:
    ticker = str(raw.get("ticker") or expected_ticker).strip().upper()
    exchange_code = str(raw.get("exchange_code") or expected_exchange_code).strip().upper()
    if ticker != expected_ticker.strip().upper() or exchange_code != expected_exchange_code.strip().upper():
        raise ValueError("instrument_mismatch")
    return LlmTriageResult(
        ticker=ticker,
        exchange_code=exchange_code,
        instrument_is_subject=bool(raw.get("instrument_is_subject", True)),
        content_type=_parse_content_type_recall_biased(raw.get("content_type")),
        reasoning=str(raw.get("reasoning") or ""),
    )


def _build_prompt(*, article, ticker: str, exchange_code: str, market_context_snapshot: dict[str, Any] | None) -> str:
    return json.dumps(
        {
            "task": (
                "Analyze whether this accepted financial news article supports a thesis "
                "card for the SPECIFIED instrument. First decide whether the instrument is "
                "genuinely a subject of the article (its company/ticker is discussed, not "
                "merely listed among many names or matched incidentally). If the instrument "
                "is not a clear subject, set instrument_is_subject=false, set relevance low, "
                "and do NOT fabricate a thesis from generic market commentary, broad "
                "'best stocks to buy' listicles, or unrelated news. Also classify the "
                "article via content_type FOR THIS INSTRUMENT: only a news_catalyst may drive a thesis."
            ),
            "instrument_grounding_rules": [
                "instrument_is_subject must be true only when the article is materially about this instrument.",
                "Generic listicles, sector roundups, or articles where the instrument is not named are NOT about it.",
                "When unsure, prefer instrument_is_subject=false and low relevance.",
            ],
            "content_type_rules": [
                "content_type is judged for the SPECIFIED instrument, not the article as a whole.",
                "content_type=news_catalyst only when the article reports a concrete, datable event about "
                "THIS instrument likely to move ITS price (e.g. its earnings, guidance change, M&A, "
                "regulatory action, product/contract news).",
                "content_type=opinion for everything else about this instrument: valuation/opinion pieces, "
                "listicles, 'best stocks to buy', price-target commentary, or analysis with no new event.",
                "If the article's event concerns a DIFFERENT company and this instrument is only discussed by "
                "comparison or commentary, that is content_type=opinion (not news_catalyst). Do NOT let another "
                "company's catalyst justify a thesis for this instrument.",
                "An opinion or valuation piece with no catalyst for this instrument must be opinion even if it "
                "reads bullishly. Do NOT label it news_catalyst.",
            ],
            "already_priced_rules": [
                "Use market_context when present to detect whether the thesis-direction move has already been realized.",
                "If a buy thesis already had a sharp positive move, or a sell thesis already had a sharp negative move, lower confidence and prefer suggested_action=hold.",
                "The deterministic ThesisBuilder gate is authoritative; this prompt instruction is advisory signal-quality guidance.",
            ],
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
            "market_context": _prompt_market_context(market_context_snapshot),
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
                "instrument_is_subject",
                "content_type",
                "evidence_bullet_candidates",
                "estimated_tokens",
            ],
        },
        sort_keys=True,
    )


def _build_triage_prompt(*, article, ticker: str, exchange_code: str) -> str:
    return json.dumps(
        {
            "task": (
                "Cheaply triage whether this accepted financial article should proceed to "
                "full thesis analysis for the SPECIFIED instrument. Reject only clear "
                "non-subjects or clear non-catalyst/opinion/listicle content. When unsure, "
                "pass through by setting instrument_is_subject=true and content_type=news_catalyst."
            ),
            "recall_bias": [
                "False negatives are more costly than false positives.",
                "If the instrument may be a real subject, pass through.",
                "If the article may contain a concrete catalyst for this instrument, pass through.",
            ],
            "instrument": {"ticker": ticker, "exchange_code": exchange_code},
            "article": {
                "id": article.id,
                "source": article.source,
                "headline": article.headline,
                "summary": article.summary,
                "tickers": article.tickers,
                "published_at": article.published_at.isoformat(),
            },
            "required_json_fields": [
                "ticker",
                "exchange_code",
                "instrument_is_subject",
                "content_type",
                "reasoning",
                "estimated_tokens",
            ],
        },
        sort_keys=True,
    )


def _get_cached_analysis(
    client: ThesisLlmClient, *, model: str, prompt: str, max_output_tokens: int
) -> dict[str, Any] | None:
    getter = getattr(client, "get_cached_analysis", None)
    if getter is None:
        return None
    cached = getter(model=model, prompt=prompt, max_output_tokens=max_output_tokens)
    if cached is None:
        return None
    if not isinstance(cached, dict):
        raise ValueError("thesis_response_not_object")
    return cached


def _actual_tokens(raw: dict[str, Any], *, fallback_tokens: int) -> int:
    value = raw.get("estimated_tokens")
    return int(value) if value is not None else fallback_tokens


def _prompt_market_context(value: Any) -> Any:
    volatile_keys = {"as_of", "quote_fetched_at", "bars_fetched_at"}
    if isinstance(value, dict):
        return {
            key: _prompt_market_context(item)
            for key, item in value.items()
            if key not in volatile_keys
        }
    if isinstance(value, list):
        return [_prompt_market_context(item) for item in value]
    return value


def _parse_content_type(value: Any) -> ContentType:
    try:
        return ContentType(str(value))
    except ValueError:
        # Default to the conservative class so unrecognized/missing values
        # never produce a thesis card.
        return ContentType.OPINION


def _parse_content_type_recall_biased(value: Any) -> ContentType:
    try:
        return ContentType(str(value))
    except ValueError:
        return ContentType.NEWS_CATALYST


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
            "instrument_is_subject",
            "content_type",
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
            "instrument_is_subject": {"type": "boolean"},
            "content_type": {
                "type": "string",
                "enum": ["news_catalyst", "opinion"],
            },
            "event_type": {"type": ["string", "null"]},
            "price_impact_magnitude": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
            "evidence_bullet_candidates": {"type": "array", "items": {"type": "string"}},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}


_TRIAGE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "thesis_builder_triage",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ticker",
            "exchange_code",
            "instrument_is_subject",
            "content_type",
            "reasoning",
            "estimated_tokens",
        ],
        "properties": {
            "ticker": {"type": "string"},
            "exchange_code": {"type": "string"},
            "instrument_is_subject": {"type": "boolean"},
            "content_type": {"type": "string", "enum": ["news_catalyst", "opinion"]},
            "reasoning": {"type": "string"},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}
