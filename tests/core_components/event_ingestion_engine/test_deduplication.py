from __future__ import annotations

from datetime import datetime, timezone

from src.core_components.event_ingestion_engine.deduplication import evaluate_dedupe
from src.core_components.event_ingestion_engine.models import CanonicalEvent, SoftDedupePolicy


def _event(
    *,
    event_id: str,
    source: str = "finnhub",
    title: str = "Company X beats estimates",
    summary: str | None = "Revenue above consensus",
    locator: str = "https://example.com/news/x",
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    occurred = occurred_at or datetime(2026, 5, 25, 10, 30, 0, tzinfo=timezone.utc)
    return CanonicalEvent(
        id=event_id,
        source=source,
        source_event_id=f"src-{event_id}",
        canonical_locator=locator,
        title=title,
        summary=summary,
        occurred_at=occurred,
        ingested_at=occurred,
    )


def test_rejects_strong_duplicate_by_id() -> None:
    incoming = _event(event_id="cev_a")
    existing = _event(event_id="cev_a")
    result = evaluate_dedupe(incoming, [existing], SoftDedupePolicy())

    assert result.accepted is False
    assert result.reason_code == "rejected_strong_duplicate"
    assert result.matched_event_id == "cev_a"


def test_rejects_soft_duplicate_when_score_at_threshold() -> None:
    incoming = _event(event_id="cev_new")
    existing = _event(event_id="cev_old")
    result = evaluate_dedupe(incoming, [existing], SoftDedupePolicy(threshold=0.85))

    assert result.accepted is False
    assert result.reason_code == "rejected_soft_duplicate"
    assert result.similarity_score is not None
    assert result.similarity_score >= 0.85
    assert result.audit["algorithm"] == "weighted_text_locator_v1"


def test_accepts_unique_when_title_overlap_is_zero() -> None:
    incoming = _event(event_id="cev_new", title="Semiconductor outlook improves")
    existing = _event(event_id="cev_old", title="Oil futures fall on inventory surprise")
    result = evaluate_dedupe(incoming, [existing], SoftDedupePolicy())

    assert result.accepted is True
    assert result.reason_code == "accepted_unique"
