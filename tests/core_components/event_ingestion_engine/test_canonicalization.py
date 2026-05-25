from __future__ import annotations

from datetime import datetime, timezone

from src.core_components.event_ingestion_engine.canonicalization import (
    canonicalize_source_event,
    generate_canonical_id,
)
from src.core_components.event_ingestion_engine.models import SourceEvent


def test_generate_canonical_id_is_stable_for_equivalent_inputs() -> None:
    occurred_at = datetime(2026, 5, 25, 10, 30, 15, tzinfo=timezone.utc)
    value_a = generate_canonical_id(
        source="finnhub",
        source_event_id="evt-1",
        canonical_locator="https://example.com/news/x",
        occurred_at=occurred_at,
        payload_version="1.0",
    )
    value_b = generate_canonical_id(
        source="finnhub",
        source_event_id="evt-1",
        canonical_locator="https://example.com/news/x",
        occurred_at=occurred_at,
        payload_version="1.0",
    )

    assert value_a.startswith("cev_")
    assert value_a == value_b


def test_canonicalize_normalizes_url_and_optional_text() -> None:
    raw = SourceEvent(
        source=" finnhub ",
        source_event_id=None,
        canonical_locator=(
            "https://EXAMPLE.com:443/news/earnings///"
            "?utm_source=x&gclid=abc&foo=bar#frag"
        ),
        title="  Company X   beats   estimates ",
        summary="   ",
        occurred_at=datetime(2026, 5, 25, 10, 30, 15, tzinfo=timezone.utc),
    )

    canonical = canonicalize_source_event(
        raw,
        ingested_at=datetime(2026, 5, 25, 10, 31, 0, tzinfo=timezone.utc),
    )

    assert canonical.source == "finnhub"
    assert canonical.source_event_id.startswith("src_")
    assert canonical.canonical_locator == "https://example.com/news/earnings?foo=bar"
    assert canonical.title == "Company X beats estimates"
    assert canonical.summary is None
