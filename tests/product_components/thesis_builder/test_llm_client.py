import pytest

from src.product_components.thesis_builder.llm_client import parse_analysis_result
from src.product_components.thesis_builder.models import (
    ContentType,
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
