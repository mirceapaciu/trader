from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.thesis_builder.export import (
    ExportedThesisCard,
    build_exported_card,
)


def _card_row(**overrides):
    row = {
        "id": "card-1",
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "direction": "buy",
        "strategy": "event_driven",
        "time_horizon": "swing_1d_5d",
        "confidence": 0.71,
        "risk_max_loss_usd": 120.0,
        "risk_stop_condition": "negative_followup_news",
        "risk_invalidation_condition": "guidance_reversal",
        "validation_status": "valid",
        "rejection_reason_code": None,
        "created_at": datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc),
        "signal_published_at": datetime(2026, 6, 15, 12, 5, tzinfo=timezone.utc),
        "evidence": [
            {"article_id": "a1", "bullet": "x"},
            {"article_id": "a2", "bullet": "y"},
        ],
    }
    row.update(overrides)
    return row


def _analysis_by_article_id():
    return {
        "a1": {
            "published_at": "2026-06-15T10:00:00+00:00",
            "fetched_at": "2026-06-15T10:01:00+00:00",
        },
        "a2": {
            "published_at": "2026-06-15T11:30:00+00:00",
            "fetched_at": "2026-06-15T11:31:00+00:00",
        },
    }


def test_build_exported_card_maps_all_fields() -> None:
    card = build_exported_card(_card_row(), _analysis_by_article_id())

    assert isinstance(card, ExportedThesisCard)
    assert card.id == "card-1"
    assert card.ticker == "AAPL"
    assert card.exchange_code == "XNAS"
    assert card.direction == "buy"
    assert card.strategy == "event_driven"
    assert card.time_horizon == "swing_1d_5d"
    assert card.confidence == 0.71
    assert card.risk_max_loss_usd == 120.0
    assert card.risk_stop_condition == "negative_followup_news"
    assert card.risk_invalidation_condition == "guidance_reversal"
    assert card.validation_status == "valid"
    assert card.rejection_reason_code is None
    assert card.created_at == datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    assert card.expires_at == datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    assert card.signal_published_at == datetime(2026, 6, 15, 12, 5, tzinfo=timezone.utc)


def test_build_exported_card_assembles_evidence_timing() -> None:
    card = build_exported_card(_card_row(), _analysis_by_article_id())

    assert [item.article_id for item in card.evidence] == ["a1", "a2"]
    assert card.evidence[0].published_at == datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    assert card.evidence[0].fetched_at == datetime(2026, 6, 15, 10, 1, tzinfo=timezone.utc)
    assert card.evidence[1].published_at == datetime(2026, 6, 15, 11, 30, tzinfo=timezone.utc)


def test_news_ready_at_is_max_published_at() -> None:
    card = build_exported_card(_card_row(), _analysis_by_article_id())

    assert card.news_ready_at == datetime(2026, 6, 15, 11, 30, tzinfo=timezone.utc)


def test_news_ready_at_falls_back_to_created_at_when_no_timing() -> None:
    card = build_exported_card(_card_row(), {})

    assert card.evidence == []
    assert card.news_ready_at == datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def test_rejected_stale_card_maps_rejection_reason_and_null_signal() -> None:
    card = build_exported_card(
        _card_row(
            validation_status="rejected",
            rejection_reason_code="stale_evidence",
            signal_published_at=None,
        ),
        _analysis_by_article_id(),
    )

    assert card.validation_status == "rejected"
    assert card.rejection_reason_code == "stale_evidence"
    assert card.signal_published_at is None


def test_evidence_entry_with_missing_timing_is_skipped() -> None:
    analysis = {"a1": {"published_at": "2026-06-15T10:00:00+00:00", "fetched_at": "2026-06-15T10:01:00+00:00"}}
    card = build_exported_card(_card_row(), analysis)

    assert [item.article_id for item in card.evidence] == ["a1"]
    assert card.news_ready_at == datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
