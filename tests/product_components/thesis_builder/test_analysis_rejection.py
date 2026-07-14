from src.product_components.thesis_builder.models import (
    ContentType,
    LlmAnalysisResult,
    SubjectRelation,
    ThesisStrategy,
    TradeDirection,
)
from src.product_components.thesis_builder.repository import (
    _already_priced_rejection,
    _analysis_rejection,
    _normalize_analysis_result,
    _tradeability_rejection,
)


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
        subject_relation=SubjectRelation.DIRECT,
        content_type=ContentType.NEWS_CATALYST,
    )
    base.update(overrides)
    return LlmAnalysisResult(**base)


def test_accepts_grounded_relevant_analysis() -> None:
    assert _analysis_rejection(_result(), min_confidence=0.6, min_relevance=0.5) is None


def test_rejects_when_instrument_not_subject() -> None:
    rejection = _analysis_rejection(
        _result(instrument_is_subject=False, subject_relation=SubjectRelation.NONE),
        min_confidence=0.6,
        min_relevance=0.5,
    )
    assert rejection == "instrument_not_subject"


def test_rejects_indirect_without_anchor_evidence() -> None:
    rejection = _analysis_rejection(
        _result(
            instrument_is_subject=False,
            subject_relation=SubjectRelation.SUPPLY_CHAIN,
            price_impact_magnitude="medium",
        ),
        min_confidence=0.6,
        min_relevance=0.5,
        has_anchor_evidence=False,
    )

    assert rejection == "indirect_no_anchor_evidence"


def test_accepts_indirect_with_anchor_evidence() -> None:
    rejection = _analysis_rejection(
        _result(
            instrument_is_subject=False,
            subject_relation=SubjectRelation.CUSTOMER_OR_PEER,
            price_impact_magnitude="medium",
        ),
        min_confidence=0.6,
        min_relevance=0.5,
        has_anchor_evidence=True,
    )

    assert rejection is None


def test_rejects_macro_relation_with_distinct_reason() -> None:
    rejection = _analysis_rejection(
        _result(instrument_is_subject=False, subject_relation=SubjectRelation.MACRO_SECTOR),
        min_confidence=0.6,
        min_relevance=0.5,
    )

    assert rejection == "macro_sector_not_subject"


def test_caps_indirect_preview_magnitude_to_low() -> None:
    result = _normalize_analysis_result(
        _result(
            instrument_is_subject=False,
            subject_relation=SubjectRelation.SUPPLY_CHAIN,
            event_type="consensus_preview",
            reasoning="TSMC is seen posting a record profit on AI demand.",
            price_impact_magnitude="high",
        )
    )

    assert result.price_impact_magnitude == "low"
    assert result.instrument_is_subject is False


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
        _result(
            instrument_is_subject=False,
            subject_relation=SubjectRelation.NONE,
            content_type=ContentType.OPINION,
        ),
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


def test_already_priced_rejects_buy_after_directional_runup() -> None:
    rejection = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.EVENT_DRIVEN, direction=TradeDirection.BUY),
        market_context_snapshot=_context(return_1d=0.05, current_price=105.0, previous_close=100.0, atr_20d=2.0),
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )

    assert rejection == "already_priced"


def test_already_priced_rejects_sell_after_directional_drop() -> None:
    rejection = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.SENTIMENT_MOMENTUM, direction=TradeDirection.SELL),
        market_context_snapshot=_context(return_1d=-0.07, current_price=93.0, previous_close=100.0, atr_20d=4.0),
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )

    assert rejection == "already_priced"


def test_already_priced_boundary_and_opposite_move_are_accepted() -> None:
    boundary = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.EVENT_DRIVEN, direction=TradeDirection.BUY),
        market_context_snapshot=_context(return_1d=0.04, current_price=104.0, previous_close=100.0, atr_20d=10.0),
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )
    opposite = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.EVENT_DRIVEN, direction=TradeDirection.BUY),
        market_context_snapshot=_context(return_1d=-0.08, current_price=92.0, previous_close=100.0, atr_20d=2.0),
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )

    assert boundary is None
    assert opposite is None


def test_already_priced_fails_closed_when_context_unusable() -> None:
    missing = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.EVENT_DRIVEN, direction=TradeDirection.BUY),
        market_context_snapshot=None,
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )
    stale = _already_priced_rejection(
        result=_result(candidate_strategy=ThesisStrategy.EVENT_DRIVEN, direction=TradeDirection.BUY),
        market_context_snapshot=_context(source_status="stale"),
        event_driven_atr_multiple=1.5,
        event_driven_return_threshold=0.04,
        sentiment_momentum_atr_multiple=2.0,
        sentiment_momentum_return_threshold=0.06,
    )

    assert missing == "market_context_unavailable"
    assert stale == "market_context_unavailable"


def test_tradeability_rejects_high_price_or_high_atr_risk() -> None:
    high_price = _tradeability_rejection(
        market_context_snapshot=_context(current_price=1200.0, atr_20d=2.0),
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )
    high_atr_risk = _tradeability_rejection(
        market_context_snapshot=_context(current_price=900.0, atr_20d=100.0),
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )

    assert high_price == "untradeable_risk_box"
    assert high_atr_risk == "untradeable_risk_box"


def test_tradeability_accepts_sizeable_context_at_boundaries() -> None:
    rejection = _tradeability_rejection(
        market_context_snapshot=_context(current_price=1000.0, atr_20d=80.0),
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )

    assert rejection is None


def test_tradeability_fails_closed_when_context_unusable() -> None:
    missing = _tradeability_rejection(
        market_context_snapshot=None,
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )
    stale = _tradeability_rejection(
        market_context_snapshot=_context(source_status="stale"),
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )
    missing_atr = _tradeability_rejection(
        market_context_snapshot=_context(atr_20d=None),
        tradeability_max_entry_price=1000.0,
        risk_max_loss_usd=120.0,
        atr_stop_mult=1.5,
    )

    assert missing == "market_context_unavailable"
    assert stale == "market_context_unavailable"
    assert missing_atr == "market_context_unavailable"


def _context(**overrides):
    base = {
        "source_status": "fresh",
        "return_1d": 0.0,
        "current_price": 100.0,
        "previous_close": 100.0,
        "atr_20d": 2.0,
    }
    base.update(overrides)
    return base
