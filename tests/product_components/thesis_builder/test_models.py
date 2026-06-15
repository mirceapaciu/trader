from datetime import datetime, timezone

from src.product_components.thesis_builder.models import ThesisCardSignal


def test_thesis_card_signal_builds_signal_envelope() -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    signal = ThesisCardSignal(
        thesis_card_id="card-1",
        ticker="AAPL",
        exchange_code="XNAS",
        direction="buy",
        time_horizon="swing_1d_5d",
        strategy="event_driven",
        confidence=0.7,
        risk_box={
            "max_loss_usd": 120,
            "stop_condition": "negative_followup_news",
            "invalidation_condition": "guidance_reversal",
        },
        source_analysis_ids=[1, 2, 3],
        created_at=now,
        expires_at=now,
    )

    envelope = signal.envelope()

    assert envelope["event_type"] == "thesis_card.created"
    assert envelope["dedupe_key"] == "card-1"
    assert envelope["payload"]["thesis_card_id"] == "card-1"
    assert envelope["payload"]["source_analysis_ids"] == [1, 2, 3]
