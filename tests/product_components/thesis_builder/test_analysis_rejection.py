from src.product_components.thesis_builder.models import (
    LlmAnalysisResult,
    ThesisStrategy,
    TradeDirection,
)
from src.product_components.thesis_builder.repository import _analysis_rejection


def _result(**overrides) -> LlmAnalysisResult:
    base = dict(
        ticker="MU",
        exchange_code="XNAS",
        sentiment=0.7,
        relevance=0.8,
        urgency="high",
        suggested_action="buy",
        candidate_strategy=ThesisStrategy.SENTIMENT_MOMENTUM,
        direction=TradeDirection.BUY,
        confidence=0.75,
        reasoning="About Micron.",
        is_market_moving=True,
        instrument_is_subject=True,
    )
    base.update(overrides)
    return LlmAnalysisResult(**base)


def test_accepts_grounded_relevant_analysis() -> None:
    assert _analysis_rejection(_result(), min_confidence=0.6, min_relevance=0.5) is None


def test_rejects_when_instrument_not_subject() -> None:
    rejection = _analysis_rejection(
        _result(instrument_is_subject=False), min_confidence=0.6, min_relevance=0.5
    )
    assert rejection == "instrument_not_subject"


def test_rejects_below_min_relevance() -> None:
    rejection = _analysis_rejection(
        _result(relevance=0.3), min_confidence=0.6, min_relevance=0.5
    )
    assert rejection == "below_min_relevance"


def test_existing_gates_still_apply() -> None:
    assert (
        _analysis_rejection(
            _result(is_market_moving=False), min_confidence=0.6, min_relevance=0.5
        )
        == "not_market_moving"
    )
    assert (
        _analysis_rejection(
            _result(confidence=0.4), min_confidence=0.6, min_relevance=0.5
        )
        == "below_min_confidence"
    )
