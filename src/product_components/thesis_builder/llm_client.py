from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Protocol

from .models import (
    ContentType,
    LlmAnalysisResult,
    LlmSynthesisResult,
    LlmStoryAssignmentResult,
    LlmTriageResult,
    StoryAssignmentCandidate,
    SubjectRelation,
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

    def analyze_synthesis(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        client = self._get_client()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _SYNTHESIS_RESPONSE_FORMAT},
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

    def analyze_story_assignment(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        client = self._get_client()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _STORY_ASSIGNMENT_RESPONSE_FORMAT},
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

    def confirm_story_event(self, *, model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        client = self._get_client()
        response = client.responses.create(
            model=model,
            input=prompt,
            instructions="Return only a JSON object that matches the requested schema.",
            max_output_tokens=max_output_tokens,
            text={"format": _STORY_EVENT_RESPONSE_FORMAT},
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
        fundamentals_snapshot: dict[str, Any] | None = None,
    ) -> LlmAnalysisResult:
        prompt = _build_prompt(
            article=article,
            ticker=ticker,
            exchange_code=exchange_code,
            market_context_snapshot=market_context_snapshot,
            fundamentals_snapshot=fundamentals_snapshot,
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


@dataclass
class ThesisCardSynthesizer:
    client: ThesisLlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    tokens_used: int = 0

    def __post_init__(self) -> None:
        self._budget_lock = threading.Lock()

    def synthesize(self, *, dossier: dict[str, Any]) -> LlmSynthesisResult:
        prompt = _build_synthesis_prompt(dossier=dossier)
        estimated_tokens = _estimate_tokens(prompt) + self.max_tokens_per_item
        self._reserve_tokens(estimated_tokens)
        try:
            caller = getattr(self.client, "analyze_synthesis", None)
            if caller is None:
                raw = self.client.analyze(
                    model=self.model,
                    prompt=prompt,
                    max_output_tokens=self.max_tokens_per_item,
                )
            else:
                raw = caller(
                    model=self.model,
                    prompt=prompt,
                    max_output_tokens=self.max_tokens_per_item,
                )
        except Exception:
            self._release_reserved_tokens(estimated_tokens)
            raise
        result = parse_synthesis_result(raw)
        actual_tokens = _actual_tokens(raw, fallback_tokens=estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        return LlmSynthesisResult(
            **{
                **result.__dict__,
                "estimated_tokens": actual_tokens,
                "llm_model": self.model,
                "raw_response": dict(raw),
            }
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


@dataclass
class ThesisStoryAssigner:
    client: ThesisLlmClient
    model: str
    max_tokens_per_run: int
    max_tokens_per_item: int
    event_check_enabled: bool = False
    tokens_used: int = 0

    def __post_init__(self) -> None:
        self._budget_lock = threading.Lock()

    def confirm_same_event(self, *, article, analysis: LlmAnalysisResult, narrative: str) -> bool | None:
        """Ask the LLM whether the incoming article and the target story are the same event.

        Used only on the ambiguous single-token-overlap band (260716-01): token overlap alone
        cannot separate "same company, same deal" from "same company, different deal". Returns
        True/False, or None to signal "no decision" (disabled, no client support, budget
        exhausted, transport error, or unparseable) so the caller can fail open to the
        deterministic result rather than crash the news loop.
        """
        if not self.event_check_enabled:
            return None
        caller = getattr(self.client, "confirm_story_event", None)
        if caller is None:
            return None
        prompt = _build_story_event_prompt(article=article, analysis=analysis, narrative=narrative)
        estimated_tokens = _estimate_tokens(prompt) + self.max_tokens_per_item
        try:
            self._reserve_tokens(estimated_tokens)
        except TokenBudgetExhausted:
            return None
        try:
            raw = caller(model=self.model, prompt=prompt, max_output_tokens=self.max_tokens_per_item)
        except Exception:
            self._release_reserved_tokens(estimated_tokens)
            return None
        actual_tokens = _actual_tokens(raw, fallback_tokens=estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        value = raw.get("same_event")
        return value if isinstance(value, bool) else None

    def assign_story(
        self,
        *,
        article,
        analysis: LlmAnalysisResult,
        candidates: list[StoryAssignmentCandidate],
    ) -> LlmStoryAssignmentResult:
        allowed_targets = {candidate.target for candidate in candidates}
        allowed_targets.add("new_story")
        prompt = _build_story_assignment_prompt(
            article=article,
            analysis=analysis,
            candidates=candidates,
        )
        estimated_tokens = _estimate_tokens(prompt) + self.max_tokens_per_item
        self._reserve_tokens(estimated_tokens)
        try:
            caller = getattr(self.client, "analyze_story_assignment", None)
            if caller is None:
                raw = self.client.analyze(
                    model=self.model,
                    prompt=prompt,
                    max_output_tokens=self.max_tokens_per_item,
                )
            else:
                raw = caller(
                    model=self.model,
                    prompt=prompt,
                    max_output_tokens=self.max_tokens_per_item,
                )
        except Exception:
            self._release_reserved_tokens(estimated_tokens)
            raise
        result = parse_story_assignment_result(raw, allowed_targets=allowed_targets)
        actual_tokens = _actual_tokens(raw, fallback_tokens=estimated_tokens)
        self._settle_tokens(reserved_tokens=estimated_tokens, actual_tokens=actual_tokens)
        return LlmStoryAssignmentResult(
            target=result.target,
            estimated_tokens=actual_tokens,
            llm_model=self.model,
            raw_response=dict(raw),
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
    subject_relation = _parse_subject_relation(
        raw.get("subject_relation"), instrument_is_subject=raw.get("instrument_is_subject")
    )
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
        instrument_is_subject=subject_relation is SubjectRelation.DIRECT,
        content_type=_parse_content_type(raw.get("content_type")),
        subject_relation=subject_relation,
        event_type=str(raw["event_type"]) if raw.get("event_type") else None,
        price_impact_magnitude=_enum_or_none(
            raw.get("price_impact_magnitude"), allowed={"low", "medium", "high"}
        ),
        impact_horizon=_enum_or_none(
            raw.get("impact_horizon"), allowed={"intraday", "1d", "5d"}
        ),
        event_occurred_at=_parse_optional_datetime(raw.get("event_occurred_at")),
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


def parse_synthesis_result(raw: dict[str, Any]) -> LlmSynthesisResult:
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in {"approve", "reject"}:
        raise ValueError("invalid_synthesis_verdict")
    confidence = _float_in_range(raw.get("confidence"), minimum=0.0, maximum=1.0, field="confidence")
    bullets = raw.get("evidence_bullets")
    if not isinstance(bullets, list):
        raise ValueError("invalid_synthesis_evidence_bullets")
    evidence_bullets = [str(item).strip() for item in bullets if str(item).strip()]
    if verdict == "approve" and not evidence_bullets:
        raise ValueError("invalid_synthesis_evidence_bullets")
    thesis_summary = str(raw.get("thesis_summary") or "").strip()
    risk_stop_condition = str(raw.get("risk_stop_condition") or "").strip()
    risk_invalidation_condition = str(raw.get("risk_invalidation_condition") or "").strip()
    if verdict == "approve" and (
        not thesis_summary or not risk_stop_condition or not risk_invalidation_condition
    ):
        raise ValueError("invalid_synthesis_approve_payload")
    return LlmSynthesisResult(
        verdict=verdict,
        confidence=confidence,
        thesis_summary=thesis_summary,
        evidence_bullets=evidence_bullets,
        risk_stop_condition=risk_stop_condition,
        risk_invalidation_condition=risk_invalidation_condition,
        risk_rationale=str(raw.get("risk_rationale") or "").strip(),
        reasoning=str(raw.get("reasoning") or "").strip(),
        reason_code=str(raw["reason_code"]).strip() if raw.get("reason_code") else None,
        raw_response=dict(raw),
    )


def parse_story_assignment_result(
    raw: dict[str, Any],
    *,
    allowed_targets: set[str],
) -> LlmStoryAssignmentResult:
    target = str(raw.get("target") or "").strip()
    if target not in allowed_targets:
        raise ValueError("invalid_story_assignment_target")
    return LlmStoryAssignmentResult(target=target)


def _build_prompt(
    *,
    article,
    ticker: str,
    exchange_code: str,
    market_context_snapshot: dict[str, Any] | None,
    fundamentals_snapshot: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "task": (
                "Analyze whether this accepted financial news article supports a thesis "
                "card for the SPECIFIED instrument. First classify the relationship between "
                "the article event and the instrument using subject_relation, then judge "
                "whether the event is material to this instrument. Do NOT fabricate a thesis "
                "from generic market commentary, broad 'best stocks to buy' listicles, or "
                "unrelated news. Also classify the article via content_type FOR THIS "
                "INSTRUMENT: only a news_catalyst may drive a thesis."
            ),
            "instrument_grounding_rules": [
                "subject_relation=direct when the article event is about the specified company's own results, guidance, operations, products, contracts, litigation, regulation, management, or securities.",
                "subject_relation=supply_chain when the event is about a supplier or customer demand channel that may read through to the instrument.",
                "subject_relation=customer_or_peer when the event is about a customer, competitor, or peer and may read through by comparison.",
                "subject_relation=macro_sector when the event is a broad macro, commodity, rates, policy, geopolitical, or sector event rather than company-specific.",
                "subject_relation=none when the instrument is only incidentally tagged or unrelated.",
                "instrument_is_subject is a compatibility field and must be true only for subject_relation=direct.",
                "When unsure, prefer subject_relation=none, instrument_is_subject=false, and low relevance.",
            ],
            "content_type_rules": [
                "content_type is judged for the SPECIFIED instrument, not the article as a whole.",
                "content_type=news_catalyst only when the article reports a concrete, datable event "
                "likely to move THIS instrument's price, including direct catalysts and material "
                "supply-chain/customer/peer read-throughs.",
                "content_type=opinion for everything else about this instrument: valuation/opinion pieces, "
                "listicles, 'best stocks to buy', price-target commentary, or analysis with no new event.",
                "If the article's event concerns a DIFFERENT company and this instrument is only discussed by "
                "comparison or commentary, that is content_type=opinion (not news_catalyst). Do NOT let another "
                "company's catalyst justify a thesis for this instrument.",
                "An opinion or valuation piece with no catalyst for this instrument must be opinion even if it "
                "reads bullishly. Do NOT label it news_catalyst.",
            ],
            "event_dating_rules": [
                "article.published_at is the upstream feed publication time, not necessarily the time of the reported event.",
                "Return event_occurred_at as the ISO 8601 date or datetime when the reported event actually occurred or was announced, based only on the headline and summary.",
                "Resolve relative expressions such as 'last week' or 'earlier this month' against article.published_at.",
                "If the text does not date the event, set event_occurred_at to null.",
                "When event_occurred_at materially predates article.published_at, treat the article as a recap rather than breaking news.",
            ],
            "already_priced_rules": [
                "Use market_context when present to detect whether the thesis-direction move has already been realized.",
                "If a buy thesis already had a sharp positive move, or a sell thesis already had a sharp negative move, lower confidence and prefer suggested_action=hold.",
                "The deterministic ThesisBuilder gate is authoritative; this prompt instruction is advisory signal-quality guidance.",
            ],
            "price_impact_rubric": [
                "price_impact_magnitude estimates the direction-aligned price move this catalyst should "
                "produce for THIS instrument over the chosen impact_horizon, measured against "
                "market_context.atr_20d: low = expected move below 0.5x atr_20d; medium = 0.5x to 1.5x "
                "atr_20d; high = above 1.5x atr_20d.",
                "When market_context or atr_20d is absent, judge the buckets relative to a typical daily "
                "trading range for this instrument.",
                "Use fundamentals (when present) to judge materiality: weigh the event's dollar scale "
                "against market_cap_usd and revenue_ttm_usd (e.g. a $500M contract is transformative for a "
                "$1B-revenue company and noise for an $80B one).",
                "A next_earnings_date close to the article date raises event risk and can amplify the "
                "expected move.",
                "Set price_impact_magnitude and impact_horizon to null only when is_market_moving is false.",
            ],
            "impact_horizon_rules": [
                "impact_horizon is the window in which most of the estimated impact should be realized: "
                "intraday = within the same trading session; 1d = by the next session close; 5d = within "
                "five trading sessions.",
            ],
            "provenance_grounding_rules": [
                "article.feed_tags are upstream feed provenance tags only; they are not attribution, "
                "not a claim that the article is about each tag, and not evidence that the specified "
                "instrument is the article subject.",
                "Ground ticker attribution, subject_relation, instrument_is_subject, and relevance in "
                "the headline and summary for the SPECIFIED instrument.",
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
                "feed_tags": article.tickers,
                "published_at": article.published_at.isoformat(),
                "sentiment_source": article.sentiment_source,
            },
            "market_context": _prompt_market_context(market_context_snapshot),
            "fundamentals": _prompt_fundamentals(fundamentals_snapshot),
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
                "event_type",
                "subject_relation",
                "event_occurred_at",
                "price_impact_magnitude",
                "impact_horizon",
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
            "provenance_grounding_rules": [
                "article.feed_tags are upstream feed provenance tags only; they are not attribution, "
                "not a claim that the article is about each tag, and not evidence that the specified "
                "instrument is the article subject.",
                "Ground instrument_is_subject and content_type in the headline and summary for the "
                "SPECIFIED instrument.",
            ],
            "instrument": {"ticker": ticker, "exchange_code": exchange_code},
            "article": {
                "id": article.id,
                "source": article.source,
                "headline": article.headline,
                "summary": article.summary,
                "feed_tags": article.tickers,
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


def _build_synthesis_prompt(*, dossier: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": (
                "Given a satisfied ThesisBuilder evidence window, decide whether the full "
                "evidence dossier justifies creating an executable thesis card. Consider "
                "coherence, corroboration, whether articles merely repeat each other, market "
                "context, deterministic gate results, and whether the move still appears actionable. "
                "Fail closed: reject when the evidence set is weak, stale, contradictory, already "
                "priced, or lacks deterministic risk text."
            ),
            "allowed_verdicts": ["approve", "reject"],
            "required_json_fields": [
                "verdict",
                "confidence",
                "thesis_summary",
                "evidence_bullets",
                "risk_stop_condition",
                "risk_invalidation_condition",
                "risk_rationale",
                "reasoning",
                "reason_code",
                "estimated_tokens",
            ],
            "dossier": dossier,
        },
        sort_keys=True,
    )


def _build_story_assignment_prompt(
    *,
    article,
    analysis: LlmAnalysisResult,
    candidates: list[StoryAssignmentCandidate],
) -> str:
    return json.dumps(
        {
            "task": (
                "Decide whether the incoming article is the same underlying news event "
                "as one existing story candidate. Choose exactly one candidate target "
                "or new_story. Do not match merely because ticker, sentiment, strategy, "
                "or direction are the same."
            ),
            "incoming_article": {
                "headline": article.headline,
                "summary": article.summary,
                "source": article.source,
                "event_type": analysis.event_type,
                "evidence_bullet_candidates": analysis.evidence_bullet_candidates,
            },
            "key": {
                "ticker": analysis.ticker,
                "exchange_code": analysis.exchange_code,
                "strategy": analysis.candidate_strategy.value,
                "direction": analysis.direction.value,
            },
            "candidates": [
                {"target": candidate.target, "story_narrative": candidate.narrative}
                for candidate in candidates
            ],
            "required_json_fields": ["target", "estimated_tokens"],
        },
        sort_keys=True,
    )


def _build_story_event_prompt(*, article, analysis: LlmAnalysisResult, narrative: str) -> str:
    return json.dumps(
        {
            "task": (
                "Do the incoming article and the existing story describe the SAME underlying "
                "news event -- the same announcement, deal, filing, report, or occurrence? "
                "Answer same_event=false if they are different events, even when they share the "
                "same company, the same counterparty, the same sector/theme, or the same "
                "sentiment or direction. Two different deals, two different analyst notes, a "
                "preview versus the actual result, or a company's own news versus a supplier's "
                "news are DIFFERENT events."
            ),
            "incoming_article": {
                "headline": article.headline,
                "summary": article.summary,
                "event_type": analysis.event_type,
                "evidence_bullet_candidates": analysis.evidence_bullet_candidates,
            },
            "existing_story": narrative,
            "required_json_fields": ["same_event", "estimated_tokens"],
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


# Snapshot keys excluded from the analysis prompt. Two groups:
#  * volatile timestamps that are never analytical signal, and
#  * quote-provenanced fields — the ones whose value depends on the live intraday
#    quote (current_price and everything derived from it) rather than on closed
#    daily bars. The live pipeline feeds a quote-bearing snapshot; the regeneration
#    backtester reconstructs context from bars only (quote=None). Dropping the
#    quote-provenanced fields makes the live and regeneration prompts byte-identical
#    for identical bar inputs, eliminating the live-vs-backtest context divergence by
#    construction. This is safe because the intraday quote was measured to be inert in
#    production (see issue 260708-03): it was absent in ~89% of analyses, never fresh,
#    and its return_1d was 0.0 in 100% of the analyses that had it — so the deterministic
#    already-priced gate (which keys off return_1d on the stored snapshot) never diverged.
_PROMPT_EXCLUDED_CONTEXT_KEYS = {
    "as_of",
    "quote_fetched_at",
    "bars_fetched_at",
    "source_status",
    "current_price",
    "previous_close",
    "return_1d",
    "return_5d",
    "return_20d",
    "drawdown_from_high_20d",
}


def _prompt_market_context(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _prompt_market_context(item)
            for key, item in value.items()
            if key not in _PROMPT_EXCLUDED_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_prompt_market_context(item) for item in value]
    return value


# The only fundamentals fields allowed into the prompt. Volatile provenance fields
# (fetched_at, last_checked_at, provider, raw payload) must stay out so that live and
# regeneration prompts are byte-identical whenever the underlying fundamentals row is
# the same (mirrors _PROMPT_EXCLUDED_CONTEXT_KEYS for market context).
_PROMPT_FUNDAMENTALS_KEYS = (
    "market_cap_usd",
    "shares_outstanding",
    "revenue_ttm_usd",
    "next_earnings_date",
)


def _prompt_fundamentals(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {key: snapshot.get(key) for key in _PROMPT_FUNDAMENTALS_KEYS}


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


def _parse_subject_relation(value: Any, *, instrument_is_subject: Any) -> SubjectRelation:
    try:
        return SubjectRelation(str(value))
    except ValueError:
        return SubjectRelation.DIRECT if bool(instrument_is_subject) else SubjectRelation.NONE


def _enum_or_none(value: Any, *, allowed: set[str]) -> str | None:
    # Cached backtester responses and fake clients may carry values outside the
    # strict schema; anything unrecognized degrades to None (which the DB CHECK
    # constraints accept) rather than failing the whole analysis.
    parsed = str(value) if value else None
    return parsed if parsed in allowed else None


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            if len(text) == 10:
                parsed = datetime.combine(date.fromisoformat(text), time.min)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
            "subject_relation",
            "event_occurred_at",
            "price_impact_magnitude",
            "impact_horizon",
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
            "subject_relation": {
                "type": "string",
                "enum": ["direct", "supply_chain", "customer_or_peer", "macro_sector", "none"],
            },
            "event_occurred_at": {"type": ["string", "null"]},
            "price_impact_magnitude": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
            "impact_horizon": {"type": ["string", "null"], "enum": ["intraday", "1d", "5d", None]},
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


_SYNTHESIS_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "thesis_builder_card_synthesis",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "confidence",
            "thesis_summary",
            "evidence_bullets",
            "risk_stop_condition",
            "risk_invalidation_condition",
            "risk_rationale",
            "reasoning",
            "reason_code",
            "estimated_tokens",
        ],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis_summary": {"type": "string"},
            "evidence_bullets": {"type": "array", "items": {"type": "string"}},
            "risk_stop_condition": {"type": "string"},
            "risk_invalidation_condition": {"type": "string"},
            "risk_rationale": {"type": "string"},
            "reasoning": {"type": "string"},
            "reason_code": {"type": ["string", "null"]},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}


_STORY_ASSIGNMENT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "thesis_builder_story_assignment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["target", "estimated_tokens"],
        "properties": {
            "target": {"type": "string"},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}


_STORY_EVENT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "thesis_builder_story_event",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["same_event", "estimated_tokens"],
        "properties": {
            "same_event": {"type": "boolean"},
            "estimated_tokens": {"type": "integer", "minimum": 0},
        },
    },
}
