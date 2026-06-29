from src.product_components.thesis_builder.models import (
    ContentType,
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
        content_type=ContentType.NEWS_CATALYST,
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


def test_not_subject_discarded_before_opinion_routing() -> None:
    # An incidental name-drop (not the subject) that also reads as opinion is dropped as
    # noise, not retained for the analyst: the subject gate precedes the content_type gate.
    rejection = _analysis_rejection(
        _result(instrument_is_subject=False, content_type=ContentType.OPINION),
        min_confidence=0.6,
        min_relevance=0.5,
    )
    assert rejection == "instrument_not_subject"


def test_opinion_routed_to_analyst_not_thesis() -> None:
    # Even a bullish, high-confidence, "market moving" opinion must not become a thesis;
    # it is retained for the stock-analyst component.
    rejection = _analysis_rejection(
        _result(content_type=ContentType.OPINION),
        min_confidence=0.6,
        min_relevance=0.5,
    )
    assert rejection == "routed_to_analyst"


def test_content_type_gate_precedes_threshold_gates() -> None:
    # Opinion classification short-circuits before strategy/relevance/confidence checks,
    # so a "screaming buy" listicle is routed to the analyst regardless of other fields.
    rejection = _analysis_rejection(
        _result(
            content_type=ContentType.OPINION,
            relevance=0.2,
            confidence=0.2,
            is_market_moving=False,
        ),
        min_confidence=0.6,
        min_relevance=0.5,
    )
    assert rejection == "routed_to_analyst"
