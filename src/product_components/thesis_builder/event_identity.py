"""Versioned event identity normalization and comparison.

The taxonomy deliberately has no catch-all family: unknown model values remain
lossless, are marked unmapped, and can be reviewed by an operator.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal

from .taxonomy_runtime import EventTaxonomySnapshot
from .taxonomy_seed import TAXONOMY_VERSION, predefined_taxonomy_snapshot


SCHEMA_VERSION = 1
_PRECISIONS = frozenset("datetime date month quarter year unknown".split())
_PERIOD_KINDS = frozenset("fiscal_quarter fiscal_half fiscal_year calendar_quarter calendar_year date_range point_in_time unknown".split())
DEFAULT_TAXONOMY_SNAPSHOT = predefined_taxonomy_snapshot()


def normalize_token(value: Any, *, limit: int = 120) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())[:limit]
    return text or None


def normalize_event_identity(raw: dict[str, Any] | None, *, ticker: str, exchange_code: str,
                             occurred_at: datetime | None = None, legacy_event_type: str | None = None,
                             taxonomy: EventTaxonomySnapshot | None = None) -> dict[str, Any]:
    """Return a safe v1 identity.  No unknown field can make analysis parsing fail."""
    snapshot = taxonomy or DEFAULT_TAXONOMY_SNAPSHOT
    source = dict(raw or {})
    # Models occasionally return a proposal field instead of the requested
    # canonical field. Treat it as input, never as a reason to lose the value.
    candidate = normalize_token(source.get("event_family")) or normalize_token(source.get("event_family_candidate"))
    if not candidate and legacy_event_type:
        candidate = normalize_token(legacy_event_type)
    family = snapshot.resolve("event_family", candidate)
    alias_subtype = snapshot.family_alias_subtypes.get(candidate or "")
    classified = family is not None
    subtype_candidate = normalize_token(source.get("event_subtype")) or normalize_token(source.get("event_subtype_candidate"))
    subtype = (
        snapshot.resolve("event_subtype", subtype_candidate, family_scope=family)
        if classified
        else None
    )
    subject = source.get("subject") if isinstance(source.get("subject"), dict) else {}
    period = source.get("period") if isinstance(source.get("period"), dict) else None
    participants = source.get("participants") if isinstance(source.get("participants"), list) else []
    safe_participants = []
    for participant in participants[:20]:
        if not isinstance(participant, dict):
            continue
        role = normalize_token(participant.get("role"))
        resolved_role = snapshot.resolve("participant_role", role)
        safe_participants.append({"role": resolved_role or "other", "role_candidate": role if role and resolved_role is None else None, "instrument_id": participant.get("instrument_id"), "name_raw": str(participant.get("name_raw") or "")[:240] or None})
    status = "classified" if classified else "unmapped"
    occurred_value = source.get("occurred_at") or (occurred_at.isoformat() if occurred_at else None)
    stage_candidate = normalize_token(source.get("event_stage")) or normalize_token(source.get("event_stage_candidate"))
    role_candidate = normalize_token(source.get("coverage_role")) or normalize_token(source.get("coverage_role_candidate"))
    stage = snapshot.resolve("event_stage", stage_candidate)
    coverage_role = snapshot.resolve("coverage_role", role_candidate)
    identity = {
        "schema_version": SCHEMA_VERSION, "taxonomy_version": TAXONOMY_VERSION,
        "taxonomy_revision": snapshot.revision,
        "classification_status": status, "event_family": family if classified else None,
        "event_family_candidate": None if classified else candidate,
        "event_subtype": subtype or (alias_subtype if classified else None),
        "event_subtype_candidate": subtype_candidate if classified and subtype is None and subtype_candidate else None,
        "event_stage": stage or "unknown",
        "event_stage_candidate": stage_candidate if stage_candidate and stage is None else None,
        "coverage_role": coverage_role or "unknown",
        "coverage_role_candidate": role_candidate if role_candidate and coverage_role is None else None,
        "subject": {"instrument_id": subject.get("instrument_id"), "ticker": str(subject.get("ticker") or ticker).upper(), "exchange_code": str(subject.get("exchange_code") or exchange_code).upper()},
        "participants": safe_participants, "period": _safe_period(period), "occurred_at": occurred_value,
        "occurred_at_precision": _controlled(source.get("occurred_at_precision"), _PRECISIONS, "unknown"),
        "identifiers": [str(value)[:240] for value in source.get("identifiers", [])[:20] if str(value).strip()],
        "attributes": source.get("attributes") if isinstance(source.get("attributes"), dict) else {},
        "confidence": _confidence(source.get("confidence")),
        "provenance": {"source": "full_analysis_llm", "prompt_version": "event-identity-v2", "taxonomy_revision": snapshot.revision, "raw_response": source},
    }
    identity["event_instance_key"] = _instance_key(identity)
    return identity


def renormalize_event_identity(
    identity: dict[str, Any],
    *,
    taxonomy: EventTaxonomySnapshot,
) -> dict[str, Any]:
    """Re-normalize a persisted identity from its lossless raw provenance."""
    provenance = (
        identity.get("provenance")
        if isinstance(identity.get("provenance"), dict)
        else {}
    )
    raw = (
        provenance.get("raw_response")
        if isinstance(provenance.get("raw_response"), dict)
        else {}
    )
    subject = (
        identity.get("subject")
        if isinstance(identity.get("subject"), dict)
        else {}
    )
    return normalize_event_identity(
        raw,
        ticker=str(subject.get("ticker") or ""),
        exchange_code=str(subject.get("exchange_code") or ""),
        legacy_event_type=None,
        taxonomy=taxonomy,
    )


def compare_event_identity(left: dict[str, Any] | None, right: dict[str, Any] | None) -> Literal["same", "different", "inconclusive"]:
    if not left or not right or left.get("classification_status") != "classified" or right.get("classification_status") != "classified":
        return "inconclusive"
    if left.get("event_instance_key") and left.get("event_instance_key") == right.get("event_instance_key"):
        return "same"
    for path in (("event_family",), ("subject", "ticker"), ("subject", "instrument_id"), ("period", "fiscal_year"), ("period", "fiscal_quarter")):
        a, b = _path(left, path), _path(right, path)
        if a is not None and b is not None and a != b:
            return "different"
    ids_left, ids_right = set(left.get("identifiers") or []), set(right.get("identifiers") or [])
    if ids_left and ids_right and not ids_left.intersection(ids_right):
        return "different"
    return "inconclusive"


def taxonomy_gap_values(identity: dict[str, Any]) -> list[tuple[str, str]]:
    gaps = []
    if identity.get("event_family_candidate"):
        gaps.append(("event_family", identity["event_family_candidate"]))
    if identity.get("event_subtype_candidate"):
        gaps.append(("event_subtype", identity["event_subtype_candidate"]))
    if identity.get("event_stage_candidate"):
        gaps.append(("event_stage", identity["event_stage_candidate"]))
    if identity.get("coverage_role_candidate"):
        gaps.append(("coverage_role", identity["coverage_role_candidate"]))
    for participant in identity.get("participants") or []:
        if isinstance(participant, dict) and participant.get("role_candidate"):
            gaps.append(("participant_role", participant["role_candidate"]))
    return gaps


def _instance_key(identity: dict[str, Any]) -> str | None:
    if identity["classification_status"] != "classified":
        return None
    subject, family = identity["subject"], identity["event_family"]
    discriminator = identity.get("identifiers") or [
        identity.get("period"),
        identity.get("occurred_at"),
    ]
    if not subject.get("ticker") or not family or not any(discriminator):
        return None
    payload = json.dumps(
        [
            subject.get("instrument_id") or subject["ticker"],
            family,
            identity.get("event_subtype"),
            discriminator,
        ],
        sort_keys=True,
        default=str,
    )
    return "v1:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _controlled(value: Any, allowed: frozenset[str], default: str) -> str:
    normalized = normalize_token(value)
    return normalized if normalized in allowed else default


def _safe_period(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {
        "kind": _controlled(value.get("kind"), _PERIOD_KINDS, "unknown"),
        "fiscal_year": value.get("fiscal_year"),
        "fiscal_quarter": value.get("fiscal_quarter"),
        "label_raw": str(value.get("label_raw") or "")[:120] or None,
    }


def _confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _path(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
