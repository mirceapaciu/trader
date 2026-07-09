import pytest
from datetime import datetime, timezone

from src.product_components.thesis_builder.llm_client import (
    ThesisAnalyzer,
    _build_prompt,
    _build_triage_prompt,
    parse_analysis_result,
    parse_triage_result,
)
from src.product_components.thesis_builder.models import (
    ContentType,
    NewsArticle,
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
