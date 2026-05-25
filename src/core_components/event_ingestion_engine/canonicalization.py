"""Canonicalization helpers and deterministic identity generation."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import CanonicalEvent, SourceEvent

_TRACKING_QUERY_KEYS = {"gclid", "fbclid"}


def to_utc(value: datetime) -> datetime:
    """Convert datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_text_required(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    return text


def _normalize_text_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value.strip())
    return text if text else None


def normalize_canonical_locator(locator: str) -> str:
    """Normalize URL-like locators according to v1 contract requirements."""
    parsed = urlsplit(locator.strip())

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if hostname and port and not default_port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    filtered_query_items: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        filtered_query_items.append((key, value))

    query = urlencode(filtered_query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _escape_pipe(value: str) -> str:
    return value.replace("|", "\\|")


def _format_rfc3339_seconds(value: datetime) -> str:
    return to_utc(value).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_canonical_id(
    *,
    source: str,
    source_event_id: str,
    canonical_locator: str,
    occurred_at: datetime,
    payload_version: str,
) -> str:
    """Generate deterministic canonical id using sha256_canonical_identity_v1."""
    parts = [
        source,
        source_event_id,
        canonical_locator,
        _format_rfc3339_seconds(occurred_at),
        payload_version,
    ]
    escaped = [_escape_pipe(part if part is not None else "") for part in parts]
    id_input = "|".join(escaped)
    digest = hashlib.sha256(id_input.encode("utf-8")).hexdigest()
    return f"cev_{digest}"


def _fallback_source_event_id(
    *,
    source: str,
    canonical_locator: str,
    title: str,
    occurred_at: datetime,
) -> str:
    fallback_input = "|".join(
        [source, canonical_locator, title, _format_rfc3339_seconds(occurred_at)]
    )
    digest = hashlib.sha256(fallback_input.encode("utf-8")).hexdigest()
    return f"src_{digest}"


def canonicalize_source_event(
    event: SourceEvent,
    *,
    ingested_at: datetime,
    payload_version: str = "1.0",
) -> CanonicalEvent:
    """Canonicalize a source event into canonical schema v1."""
    source = _normalize_text_required(event.source)
    title = _normalize_text_required(event.title)
    occurred_at = to_utc(event.occurred_at)
    normalized_locator = normalize_canonical_locator(event.canonical_locator)

    source_event_id = _normalize_text_optional(event.source_event_id)
    if source_event_id is None:
        source_event_id = _fallback_source_event_id(
            source=source,
            canonical_locator=normalized_locator,
            title=title,
            occurred_at=occurred_at,
        )

    event_id = generate_canonical_id(
        source=source,
        source_event_id=source_event_id,
        canonical_locator=normalized_locator,
        occurred_at=occurred_at,
        payload_version=payload_version,
    )

    return CanonicalEvent(
        id=event_id,
        source=source,
        source_event_id=source_event_id,
        canonical_locator=normalized_locator,
        title=title,
        summary=_normalize_text_optional(event.summary),
        content_text=_normalize_text_optional(event.content_text),
        occurred_at=occurred_at,
        ingested_at=to_utc(ingested_at),
        payload_version=payload_version,
        entities=event.entities,
        attributes=event.attributes,
        extensions=event.extensions,
    )
