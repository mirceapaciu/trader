import json

import pytest
from datetime import datetime, timezone

from src.product_components.thesis_builder.llm_client import (
    ThesisAnalyzer,
    ThesisStoryAssigner,
    _THESIS_ANALYSIS_RESPONSE_FORMAT,
    _build_prompt,
    _build_triage_prompt,
    parse_analysis_result,
    parse_story_assignment_result,
    parse_synthesis_result,
    parse_triage_result,
)
from src.product_components.thesis_builder.models import (
    ContentType,
    LlmAnalysisResult,
    NewsArticle,
    StoryAssignmentCandidate,
    SubjectRelation,
    ThesisStrategy,
    TradeDirection,
)


def test_parse_analysis_result_validates_structured_response() -> None:
    result = parse_analysis_result(
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "sentiment": 0.8,
            "relevance": 0.9,
            "urgency": "today",
            "suggested_action": "buy",
            "candidate_strategy": "event_driven",
            "direction": "buy",
            "confidence": 0.75,
            "reasoning": "Guidance improved.",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "subject_relation": "direct",
            "event_type": "guidance",
            "price_impact_magnitude": "medium",
            "content_type": "news_catalyst",
            "evidence_bullet_candidates": ["Guidance improved."],
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.candidate_strategy is ThesisStrategy.EVENT_DRIVEN
    assert result.direction is TradeDirection.BUY
    assert result.confidence == 0.75
    assert result.instrument_is_subject is True
    assert result.subject_relation is SubjectRelation.DIRECT
    assert result.content_type is ContentType.NEWS_CATALYST


def test_parse_analysis_result_parses_opinion() -> None:
    result = parse_analysis_result(
        {
            "ticker": "MSFT",
            "exchange_code": "XNAS",
            "sentiment": 0.5,
            "relevance": 0.6,
            "urgency": "informational",
            "suggested_action": "buy",
            "candidate_strategy": "sentiment_momentum",
            "direction": "buy",
            "confidence": 0.7,
            "reasoning": "Undervalued vs peers on a DCF basis.",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "subject_relation": "direct",
            "content_type": "opinion",
        },
        expected_ticker="MSFT",
        expected_exchange_code="XNAS",
    )

    assert result.content_type is ContentType.OPINION


def test_parse_analysis_result_defaults_content_type_to_opinion() -> None:
    # Missing or unrecognized content_type defaults to the conservative class so
    # it can never produce a thesis card.
    missing = parse_analysis_result(
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "sentiment": 0.1,
            "relevance": 0.2,
            "urgency": "informational",
            "suggested_action": "hold",
            "candidate_strategy": "sentiment_momentum",
            "direction": "hold",
            "confidence": 0.4,
            "reasoning": "Generic listicle.",
            "is_market_moving": False,
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )
    assert missing.content_type is ContentType.OPINION

    invalid = parse_analysis_result(
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "sentiment": 0.1,
            "relevance": 0.2,
            "urgency": "informational",
            "suggested_action": "hold",
            "candidate_strategy": "sentiment_momentum",
            "direction": "hold",
            "confidence": 0.4,
            "reasoning": "Generic listicle.",
            "is_market_moving": False,
            "content_type": "editorial",
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )
    assert invalid.content_type is ContentType.OPINION


def test_parse_analysis_result_defaults_instrument_is_subject_false() -> None:
    result = parse_analysis_result(
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "sentiment": 0.1,
            "relevance": 0.2,
            "urgency": "informational",
            "suggested_action": "hold",
            "candidate_strategy": "sentiment_momentum",
            "direction": "hold",
            "confidence": 0.4,
            "reasoning": "Generic market listicle, not about this instrument.",
            "is_market_moving": False,
            # instrument_is_subject intentionally omitted -> conservative default False
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.instrument_is_subject is False
    assert result.subject_relation is SubjectRelation.NONE


def test_parse_analysis_result_maps_indirect_relation_and_derives_subject_boolean() -> None:
    result = parse_analysis_result(
        {
            "ticker": "NVDA",
            "exchange_code": "XNAS",
            "sentiment": 0.6,
            "relevance": 0.7,
            "urgency": "today",
            "suggested_action": "buy",
            "candidate_strategy": "event_driven",
            "direction": "buy",
            "confidence": 0.75,
            "reasoning": "TSMC AI demand reads through to Nvidia.",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "subject_relation": "supply_chain",
            "content_type": "news_catalyst",
        },
        expected_ticker="NVDA",
        expected_exchange_code="XNAS",
    )

    assert result.subject_relation is SubjectRelation.SUPPLY_CHAIN
    assert result.instrument_is_subject is False


def test_parse_analysis_result_rejects_instrument_mismatch() -> None:
    with pytest.raises(ValueError, match="instrument_mismatch"):
        parse_analysis_result(
            {
                "ticker": "MSFT",
                "exchange_code": "XNAS",
                "sentiment": 0.8,
                "relevance": 0.9,
                "urgency": "today",
                "suggested_action": "buy",
                "candidate_strategy": "event_driven",
                "direction": "buy",
                "confidence": 0.75,
                "reasoning": "Guidance improved.",
                "is_market_moving": True,
            },
            expected_ticker="AAPL",
            expected_exchange_code="XNAS",
        )


def test_parse_analysis_result_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="invalid_confidence"):
        parse_analysis_result(
            {
                "ticker": "AAPL",
                "exchange_code": "XNAS",
                "sentiment": 0.8,
                "relevance": 0.9,
                "urgency": "today",
                "suggested_action": "buy",
                "candidate_strategy": "event_driven",
                "direction": "buy",
                "confidence": 1.5,
                "reasoning": "Guidance improved.",
                "is_market_moving": True,
            },
            expected_ticker="AAPL",
            expected_exchange_code="XNAS",
        )


def test_build_prompt_omits_volatile_market_context_timestamps() -> None:
    article = _article()
    context_a = {
        "as_of": "2025-01-02T14:00:00+00:00",
        "quote_fetched_at": "2025-01-02T13:59:58+00:00",
        "bars_fetched_at": "2025-01-02T13:59:00+00:00",
        "trend": {"sma_20": 101.25},
    }
    context_b = {
        "as_of": "2025-01-02T14:05:00+00:00",
        "quote_fetched_at": "2025-01-02T14:04:58+00:00",
        "bars_fetched_at": "2025-01-02T14:04:00+00:00",
        "trend": {"sma_20": 101.25},
    }

    prompt_a = _build_prompt(
        article=article,
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot=context_a,
    )
    prompt_b = _build_prompt(
        article=article,
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot=context_b,
    )

    assert prompt_a == prompt_b
    assert "quote_fetched_at" not in prompt_a
    assert "bars_fetched_at" not in prompt_a
    assert '"as_of"' not in prompt_a
    assert context_a["as_of"] == "2025-01-02T14:00:00+00:00"


def test_build_prompt_keeps_market_context_numbers_cache_sensitive() -> None:
    article = _article()
    prompt_a = _build_prompt(
        article=article,
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot={"trend": {"sma_20": 101.25}},
    )
    prompt_b = _build_prompt(
        article=article,
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot={"trend": {"sma_20": 102.25}},
    )

    assert prompt_a != prompt_b


def test_build_prompt_identical_for_quote_bearing_and_bar_only_context() -> None:
    # Issue 260708-03: the live path builds market context from a quote + bars, while
    # the regeneration backtester reconstructs it from bars only (quote=None). The
    # analysis prompt must be byte-identical for identical bars so live and backtest
    # analyses never diverge on context. Use a fresh realtime quote whose last_price
    # differs from the last bar close (the worst case for divergence).
    import json
    from dataclasses import asdict
    from datetime import datetime, timedelta, timezone

    from src.product_components.market_data.context import build_market_context
    from src.product_components.market_data.models import (
        MarketBar,
        MarketQuote,
        QuoteDataType,
    )

    now = datetime(2025, 1, 2, 14, 0, tzinfo=timezone.utc)
    bars = [
        MarketBar(
            ticker="AAPL",
            exchange_code="XNAS",
            provider="test",
            bar_interval="1d",
            bar_start_at=now - timedelta(days=30 - i),
            currency="USD",
            open_price=100.0 + i,
            high_price=101.0 + i,
            low_price=99.0 + i,
            close_price=100.0 + i,
            volume=1_000 + i,
            adjusted=False,
            fetched_at=now,
            provider_metadata={},
        )
        for i in range(30)
    ]
    quote = MarketQuote(
        ticker="AAPL",
        exchange_code="XNAS",
        provider="test",
        data_type=QuoteDataType.REALTIME,
        currency="USD",
        bid_price=128.0,
        ask_price=128.2,
        last_price=128.1,  # deliberately far from the last bar close (129.0)
        previous_close=127.0,
        volume=2_000,
        provider_timestamp=now,
        fetched_at=now,
        provider_metadata={},
    )

    def _ctx(quote_arg):
        snap = build_market_context(
            ticker="AAPL",
            exchange_code="XNAS",
            quote=quote_arg,
            bars=bars,
            now=now,
            quote_max_age_seconds=300,
        )
        return json.loads(json.dumps(asdict(snap), default=str, sort_keys=True))

    live_ctx = _ctx(quote)
    backtest_ctx = _ctx(None)
    # The raw snapshots differ on the quote-provenanced fields...
    assert live_ctx["current_price"] != backtest_ctx["current_price"]
    assert live_ctx["source_status"] != backtest_ctx["source_status"]

    article = _article()
    live_prompt = _build_prompt(
        article=article, ticker="AAPL", exchange_code="XNAS", market_context_snapshot=live_ctx
    )
    backtest_prompt = _build_prompt(
        article=article, ticker="AAPL", exchange_code="XNAS", market_context_snapshot=backtest_ctx
    )
    # ...but the prompts fed to the LLM are byte-identical.
    assert live_prompt == backtest_prompt
    # The bar-derived features survive; the quote-provenanced ones are gone.
    assert '"sma_20d"' in live_prompt
    assert "current_price" not in live_prompt
    assert "return_1d" not in live_prompt


def test_build_prompt_includes_already_priced_advisory() -> None:
    prompt = _build_prompt(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot={"source_status": "fresh", "return_1d": 0.08},
    )

    assert "already had a sharp positive move" in prompt
    assert "deterministic ThesisBuilder gate is authoritative" in prompt


def test_build_prompt_labels_article_tickers_as_feed_tags_provenance() -> None:
    prompt = _build_prompt(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot=None,
    )
    payload = json.loads(prompt)

    assert payload["article"]["feed_tags"] == ["AAPL"]
    assert "tickers" not in payload["article"]
    provenance_rules = " ".join(payload["provenance_grounding_rules"])
    assert "feed provenance tags only" in provenance_rules
    assert "not attribution" in provenance_rules
    assert "not evidence that the specified instrument is the article subject" in provenance_rules


def test_cached_analysis_does_not_reserve_or_consume_token_budget() -> None:
    analyzer = ThesisAnalyzer(
        client=_CachedClient(),
        model="model-a",
        max_tokens_per_run=1,
        max_tokens_per_item=1200,
    )

    result = analyzer.analyze_article(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot={"trend": {"sma_20": 101.25}},
    )

    assert result.estimated_tokens == 0
    assert analyzer.tokens_used == 0


def test_triage_prompt_is_recall_biased() -> None:
    prompt = _build_triage_prompt(article=_article(), ticker="AAPL", exchange_code="XNAS")

    assert "When unsure" in prompt
    assert "pass through" in prompt


def test_triage_prompt_labels_article_tickers_as_feed_tags_provenance() -> None:
    prompt = _build_triage_prompt(article=_article(), ticker="AAPL", exchange_code="XNAS")
    payload = json.loads(prompt)

    assert payload["article"]["feed_tags"] == ["AAPL"]
    assert "tickers" not in payload["article"]
    provenance_rules = " ".join(payload["provenance_grounding_rules"])
    assert "feed provenance tags only" in provenance_rules
    assert "not attribution" in provenance_rules
    assert "not evidence that the specified instrument is the article subject" in provenance_rules


def test_parse_triage_result_defaults_missing_content_type_to_pass_through() -> None:
    result = parse_triage_result(
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "instrument_is_subject": True,
            "reasoning": "Ambiguous but possibly about Apple.",
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.content_type is ContentType.NEWS_CATALYST


def test_parse_synthesis_result_accepts_approve_payload() -> None:
    result = parse_synthesis_result(
        {
            "verdict": "approve",
            "confidence": 0.82,
            "thesis_summary": "Guidance raise supports a swing buy thesis.",
            "evidence_bullets": ["Guidance was raised.", "Management cited durable demand."],
            "risk_stop_condition": "close_below_post_guidance_support",
            "risk_invalidation_condition": "company_reverses_guidance",
            "risk_rationale": "Invalidation is tied to the reported catalyst.",
            "reasoning": "Evidence corroborates across sources.",
            "reason_code": None,
            "estimated_tokens": 321,
        }
    )

    assert result.verdict == "approve"
    assert result.confidence == 0.82
    assert result.evidence_bullets == [
        "Guidance was raised.",
        "Management cited durable demand.",
    ]
    assert result.risk_stop_condition == "close_below_post_guidance_support"


def test_parse_synthesis_result_accepts_reject_payload() -> None:
    result = parse_synthesis_result(
        {
            "verdict": "reject",
            "confidence": 0.2,
            "thesis_summary": "",
            "evidence_bullets": [],
            "risk_stop_condition": "",
            "risk_invalidation_condition": "",
            "risk_rationale": "",
            "reasoning": "Articles repeat the same weak opinion.",
            "reason_code": "weak_corroboration",
            "estimated_tokens": 111,
        }
    )

    assert result.verdict == "reject"
    assert result.reason_code == "weak_corroboration"


def test_parse_synthesis_result_rejects_malformed_approve_payload() -> None:
    with pytest.raises(ValueError, match="invalid_synthesis_approve_payload"):
        parse_synthesis_result(
            {
                "verdict": "approve",
                "confidence": 0.8,
                "thesis_summary": "",
                "evidence_bullets": ["evidence"],
                "risk_stop_condition": "",
                "risk_invalidation_condition": "",
                "risk_rationale": "",
                "reasoning": "Missing risk fields.",
                "reason_code": None,
                "estimated_tokens": 1,
            }
        )


def test_triage_uses_same_token_budget_counter() -> None:
    analyzer = ThesisAnalyzer(
        client=_TriageClient(),
        model="analysis-model",
        max_tokens_per_run=10000,
        max_tokens_per_item=1200,
        triage_model="triage-model",
        triage_max_output_tokens=100,
    )

    result = analyzer.triage_article(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
    )

    assert result.estimated_tokens == 37
    assert result.llm_model == "triage-model"
    assert analyzer.tokens_used == 37


def test_parse_story_assignment_result_rejects_unknown_target() -> None:
    assert parse_story_assignment_result(
        {"target": "window:7", "estimated_tokens": 5},
        allowed_targets={"window:7", "new_story"},
    ).target == "window:7"
    with pytest.raises(ValueError, match="invalid_story_assignment_target"):
        parse_story_assignment_result(
            {"target": "window:8", "estimated_tokens": 5},
            allowed_targets={"window:7", "new_story"},
        )


def test_story_assigner_uses_story_assignment_endpoint_and_budget() -> None:
    assigner = ThesisStoryAssigner(
        client=_StoryAssignmentClient(),
        model="story-model",
        max_tokens_per_run=10000,
        max_tokens_per_item=90,
    )

    result = assigner.assign_story(
        article=_article(),
        analysis=_story_analysis(),
        candidates=[StoryAssignmentCandidate(target="window:1", narrative="Guidance raise")],
    )

    assert result.target == "window:1"
    assert result.estimated_tokens == 29
    assert result.llm_model == "story-model"
    assert assigner.tokens_used == 29


class _CachedClient:
    def get_cached_analysis(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        return {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "sentiment": 0.8,
            "relevance": 0.9,
            "urgency": "today",
            "suggested_action": "buy",
            "candidate_strategy": "event_driven",
            "direction": "buy",
            "confidence": 0.75,
            "reasoning": "Guidance improved.",
            "is_market_moving": True,
            "instrument_is_subject": True,
            "content_type": "news_catalyst",
            "evidence_bullet_candidates": ["Guidance improved."],
            "estimated_tokens": 0,
        }

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        raise AssertionError("cached analyzer path must not delegate")


class _TriageClient:
    def analyze_triage(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        assert model == "triage-model"
        assert max_output_tokens == 100
        return {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "instrument_is_subject": False,
            "content_type": "news_catalyst",
            "reasoning": "Incidental mention.",
            "estimated_tokens": 37,
        }

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        raise AssertionError("triage should use analyze_triage")


class _StoryAssignmentClient:
    def analyze_story_assignment(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        assert model == "story-model"
        assert max_output_tokens == 90
        assert "window:1" in prompt
        return {"target": "window:1", "estimated_tokens": 29}

    def analyze(self, *, model: str, prompt: str, max_output_tokens: int) -> dict:
        raise AssertionError("story assignment should use analyze_story_assignment")


def _story_analysis() -> LlmAnalysisResult:
    return LlmAnalysisResult(
        ticker="AAPL",
        exchange_code="XNAS",
        sentiment=0.8,
        relevance=0.9,
        urgency="today",
        suggested_action="buy",
        candidate_strategy=ThesisStrategy.EVENT_DRIVEN,
        direction=TradeDirection.BUY,
        confidence=0.75,
        reasoning="Guidance improved.",
        is_market_moving=True,
        instrument_is_subject=True,
        content_type=ContentType.NEWS_CATALYST,
        subject_relation=SubjectRelation.DIRECT,
        event_type="guidance",
        evidence_bullet_candidates=["Guidance improved."],
    )


def _analysis_payload(**overrides) -> dict:
    payload = {
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "sentiment": 0.8,
        "relevance": 0.9,
        "urgency": "today",
        "suggested_action": "buy",
        "candidate_strategy": "event_driven",
        "direction": "buy",
        "confidence": 0.75,
        "reasoning": "Guidance improved.",
        "is_market_moving": True,
        "instrument_is_subject": True,
        "subject_relation": "direct",
        "content_type": "news_catalyst",
    }
    payload.update(overrides)
    return payload


def test_parse_analysis_result_parses_impact_horizon() -> None:
    result = parse_analysis_result(
        _analysis_payload(price_impact_magnitude="high", impact_horizon="1d"),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.price_impact_magnitude == "high"
    assert result.impact_horizon == "1d"


def test_parse_analysis_result_parses_event_occurred_at() -> None:
    dated = parse_analysis_result(
        _analysis_payload(event_occurred_at="2026-07-09"),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )
    timed = parse_analysis_result(
        _analysis_payload(event_occurred_at="2026-07-09T16:30:00-04:00"),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert dated.event_occurred_at == datetime(2026, 7, 9, tzinfo=timezone.utc)
    assert timed.event_occurred_at == datetime(2026, 7, 9, 20, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_analysis_result_degrades_invalid_event_occurred_at_to_null(value) -> None:
    result = parse_analysis_result(
        _analysis_payload(event_occurred_at=value),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.event_occurred_at is None


def test_parse_analysis_result_normalizes_invalid_impact_fields() -> None:
    # Cached backtester responses predate the field or may carry values outside
    # the strict schema; anything unrecognized degrades to None so the DB CHECK
    # constraints never reject the analysis.
    missing = parse_analysis_result(
        _analysis_payload(),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )
    assert missing.price_impact_magnitude is None
    assert missing.impact_horizon is None

    invalid = parse_analysis_result(
        _analysis_payload(price_impact_magnitude="extreme", impact_horizon="2w"),
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )
    assert invalid.price_impact_magnitude is None
    assert invalid.impact_horizon is None
    assert invalid.event_occurred_at is None


def test_analysis_response_schema_required_matches_properties() -> None:
    # OpenAI strict json_schema mode fails EVERY request when a property is
    # missing from `required` (or vice versa); this invariant guards the
    # add-a-field-in-one-place failure mode permanently.
    schema = _THESIS_ANALYSIS_RESPONSE_FORMAT["schema"]
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_build_prompt_includes_impact_rubric() -> None:
    prompt = _build_prompt(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot={"atr_20d": 2.5},
    )

    assert '"price_impact_rubric"' in prompt
    assert '"impact_horizon_rules"' in prompt
    assert "atr_20d" in prompt
    # The new output fields are demanded from the model explicitly.
    for field in ("event_type", "subject_relation", "event_occurred_at", "price_impact_magnitude", "impact_horizon"):
        assert f'"{field}"' in prompt


def test_build_prompt_includes_event_dating_rules_only_in_full_analysis() -> None:
    prompt = _build_prompt(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot=None,
    )
    payload = json.loads(prompt)

    assert "event_occurred_at" in payload["required_json_fields"]
    event_rules = " ".join(payload["event_dating_rules"])
    assert "published_at is the upstream feed publication time" in event_rules
    assert "last week" in event_rules

    triage_prompt = _build_triage_prompt(article=_article(), ticker="AAPL", exchange_code="XNAS")
    assert "event_occurred_at" not in triage_prompt
    assert "event_dating_rules" not in triage_prompt


def test_build_prompt_fundamentals_block_whitelists_stable_fields() -> None:
    article = _article()
    base = {
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "provider": "finnhub",
        "market_cap_usd": 3.1e12,
        "shares_outstanding": 1.5e10,
        "revenue_ttm_usd": 4.0e11,
        "next_earnings_date": "2026-07-30",
        "fetched_at": "2026-07-10T04:00:00+00:00",
        "last_checked_at": "2026-07-13T04:00:00+00:00",
    }
    later_check = {**base, "fetched_at": "2026-07-11T04:00:00+00:00", "last_checked_at": "2026-07-14T04:00:00+00:00"}

    prompt_a = _build_prompt(
        article=article, ticker="AAPL", exchange_code="XNAS",
        market_context_snapshot=None, fundamentals_snapshot=base,
    )
    prompt_b = _build_prompt(
        article=article, ticker="AAPL", exchange_code="XNAS",
        market_context_snapshot=None, fundamentals_snapshot=later_check,
    )

    # Snapshots differing only in provenance timestamps produce byte-identical
    # prompts (live/regeneration parity).
    assert prompt_a == prompt_b
    assert "fetched_at" not in prompt_a
    assert "last_checked_at" not in prompt_a
    assert '"finnhub"' not in prompt_a
    assert '"market_cap_usd": 3100000000000.0' in prompt_a
    assert '"next_earnings_date": "2026-07-30"' in prompt_a

    # Value changes must change the prompt (cache-sensitive).
    changed = _build_prompt(
        article=article, ticker="AAPL", exchange_code="XNAS",
        market_context_snapshot=None,
        fundamentals_snapshot={**base, "market_cap_usd": 3.2e12},
    )
    assert changed != prompt_a


def test_build_prompt_fundamentals_null_when_missing() -> None:
    prompt = _build_prompt(
        article=_article(),
        ticker="AAPL",
        exchange_code="XNAS",
        market_context_snapshot=None,
    )

    # The key is always present so prompt structure is deterministic; a missing
    # snapshot renders as null.
    assert '"fundamentals": null' in prompt


def _article() -> NewsArticle:
    at = datetime(2025, 1, 2, 14, 0, tzinfo=timezone.utc)
    return NewsArticle(
        id="article-1",
        source="wire",
        headline="Apple raises guidance",
        summary="Apple raises guidance after strong demand.",
        url="https://example.test/apple-guidance",
        tickers=["AAPL"],
        published_at=at,
        fetched_at=at,
        sentiment_source=0.7,
    )
