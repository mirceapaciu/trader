from datetime import datetime, timedelta, timezone

from src.product_components.trade_executor.models import DecisionReason, ThesisCard
from src.product_components.trade_executor.pipeline import evaluate_admission_gate

NOW = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)
HORIZON_MAP = {"swing_1d_5d": 5}


def _card(**overrides) -> ThesisCard:
    base = dict(
        thesis_card_id="card-1",
        ticker="AAPL",
        exchange_code="XNAS",
        direction="buy",
        time_horizon="swing_1d_5d",
        strategy="event_driven",
        confidence=0.8,
        max_loss_usd=120.0,
        stop_condition="close_below_recent_support",
        invalidation_condition=None,
        source_analysis_ids=[1, 2, 3],
        created_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
    )
    base.update(overrides)
    return ThesisCard(**base)


def _gate(card: ThesisCard, **overrides):
    kwargs = dict(
        card=card,
        now=NOW,
        min_confidence=0.6,
        in_watchlist=True,
        review_state="approved",
        has_open_or_working_position=False,
        horizon_map=HORIZON_MAP,
    )
    kwargs.update(overrides)
    return evaluate_admission_gate(**kwargs)


def test_admits_valid_buy() -> None:
    outcome = _gate(_card())
    assert outcome.passed is True
    assert outcome.reason == DecisionReason.ADMITTED


def test_admits_valid_sell_short() -> None:
    outcome = _gate(_card(direction="sell"))
    assert outcome.passed is True


def test_direction_hold_dropped() -> None:
    outcome = _gate(_card(direction="hold"))
    assert outcome.reason == DecisionReason.DIRECTION_HOLD
    assert outcome.passed is False


def test_expired_card_dropped() -> None:
    outcome = _gate(_card(expires_at=NOW - timedelta(minutes=1)))
    assert outcome.reason == DecisionReason.CARD_EXPIRED


def test_below_min_confidence_dropped() -> None:
    outcome = _gate(_card(confidence=0.5))
    assert outcome.reason == DecisionReason.BELOW_MIN_CONFIDENCE


def test_not_in_watchlist_dropped() -> None:
    outcome = _gate(_card(), in_watchlist=False)
    assert outcome.reason == DecisionReason.NOT_IN_WATCHLIST


def test_position_exists_dropped() -> None:
    outcome = _gate(_card(), has_open_or_working_position=True)
    assert outcome.reason == DecisionReason.POSITION_EXISTS


def test_review_not_approved_dropped() -> None:
    outcome = _gate(_card(), review_state="rejected")
    assert outcome.reason == DecisionReason.REVIEW_NOT_APPROVED


def test_missing_review_treated_as_rejected() -> None:
    outcome = _gate(_card(), review_state=None)
    assert outcome.reason == DecisionReason.REVIEW_NOT_APPROVED


def test_horizon_unmapped_dropped() -> None:
    outcome = _gate(_card(time_horizon="scalp_1m"))
    assert outcome.reason == DecisionReason.HORIZON_UNMAPPED


def test_gate_check_order_hold_before_expiry() -> None:
    # A hold card that is also expired reports the earlier check (direction_hold).
    outcome = _gate(_card(direction="hold", expires_at=NOW - timedelta(minutes=1)))
    assert outcome.reason == DecisionReason.DIRECTION_HOLD
