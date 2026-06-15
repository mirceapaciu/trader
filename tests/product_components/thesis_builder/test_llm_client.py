import pytest

from src.product_components.thesis_builder.llm_client import parse_analysis_result
from src.product_components.thesis_builder.models import ThesisStrategy, TradeDirection


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
            "event_type": "guidance",
            "price_impact_magnitude": "medium",
            "evidence_bullet_candidates": ["Guidance improved."],
        },
        expected_ticker="AAPL",
        expected_exchange_code="XNAS",
    )

    assert result.candidate_strategy is ThesisStrategy.EVENT_DRIVEN
    assert result.direction is TradeDirection.BUY
    assert result.confidence == 0.75


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
