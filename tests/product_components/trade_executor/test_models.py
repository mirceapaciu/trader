from datetime import timezone

from src.product_components.trade_executor.models import SignalMessage


def _msg(payload: dict, event_type: str = "thesis_card.created") -> SignalMessage:
    return SignalMessage(
        message_id="1-0",
        event_id="evt",
        event_type=event_type,
        dedupe_key="c1",
        payload=payload,
    )


def _valid_payload(**overrides) -> dict:
    payload = {
        "thesis_card_id": "c1",
        "ticker": "aapl",
        "exchange_code": "xnas",
        "direction": "BUY",
        "time_horizon": "swing_1d_5d",
        "strategy": "event_driven",
        "confidence": 0.8,
        "risk_box": {
            "max_loss_usd": 120.0,
            "stop_condition": "close_below_recent_support",
            "invalidation_condition": "thesis_broken",
        },
        "source_analysis_ids": [1, 2, "3"],
        "created_at": "2026-07-03T14:00:00Z",
        "expires_at": "2026-07-03T18:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_parses_valid_card_and_normalizes() -> None:
    card = _msg(_valid_payload()).as_thesis_card()
    assert card is not None
    assert card.ticker == "AAPL"
    assert card.exchange_code == "XNAS"
    assert card.direction == "buy"
    assert card.max_loss_usd == 120.0
    assert card.source_analysis_ids == [1, 2, 3]
    assert card.created_at.tzinfo == timezone.utc
    assert card.stop_condition == "close_below_recent_support"


def test_missing_required_field_returns_none() -> None:
    payload = _valid_payload()
    del payload["ticker"]
    assert _msg(payload).as_thesis_card() is None


def test_missing_max_loss_returns_none() -> None:
    payload = _valid_payload(risk_box={"stop_condition": "x"})
    assert _msg(payload).as_thesis_card() is None


def test_bad_datetime_returns_none() -> None:
    payload = _valid_payload(expires_at="not-a-date")
    assert _msg(payload).as_thesis_card() is None


def test_is_thesis_card_flag() -> None:
    assert _msg({}, event_type="thesis_card.created").is_thesis_card is True
    assert _msg({}, event_type="other.event").is_thesis_card is False
