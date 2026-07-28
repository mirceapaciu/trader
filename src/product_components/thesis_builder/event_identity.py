"""Versioned event identity normalization and comparison.

The taxonomy deliberately has no catch-all family: unknown model values remain
lossless, are marked unmapped, and can be reviewed by an operator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal


SCHEMA_VERSION = 1
TAXONOMY_VERSION = "event-taxonomy-v1"

EVENT_FAMILIES = frozenset("""earnings_results guidance_outlook investor_day_strategy accounting_audit_restatement dividend share_repurchase financing_debt equity_offering credit_rating bankruptcy_restructuring merger_acquisition divestiture_spin_off ownership_stake_transaction partnership_joint_venture management_change governance_shareholder_action security_corporate_action listing_index_change commercial_contract_order government_award_subsidy capital_investment_capacity product_service_launch retail_sales_promotion_event product_pricing_change commercial_metrics production_operations_update supply_chain_disruption workforce_labor operational_incident_outage cybersecurity_incident product_recall_safety regulatory_approval regulatory_investigation_enforcement litigation_judgment_settlement intellectual_property filing_disclosure clinical_trial_result drug_device_regulatory investment_research_report analyst_rating_price_target digital_asset_event macroeconomic_data monetary_policy fiscal_trade_policy geopolitical_event political_election_transition natural_disaster_weather_event commodity_market_event sector_industry_development market_move""".split())

SUBTYPES = {
    "earnings_results": frozenset("quarterly_results half_year_results annual_results preliminary_results earnings_call".split()),
    "fiscal_trade_policy": frozenset("sanctions export_controls tariff subsidy tax_policy".split()),
    "geopolitical_event": frozenset("military_action military_conflict diplomatic_agreement ceasefire peace_deal".split()),
    "retail_sales_promotion_event": frozenset("prime_day black_friday cyber_monday other_sales_event".split()),
    "digital_asset_event": frozenset("protocol_upgrade stablecoin_authorization exchange_or_broker_launch token_regulatory_action digital_asset_market_event".split()),
    "investment_research_report": frozenset("short_seller_report activist_report independent_research".split()),
    "political_election_transition": frozenset("election government_formation leadership_transition referendum".split()),
    "natural_disaster_weather_event": frozenset("hurricane earthquake wildfire flood tornado extreme_weather".split()),
}
_STAGES = frozenset("proposed scheduled announced pending approved in_progress completed cancelled denied corrected unknown".split())
_ROLES = frozenset("primary_announcement results_report preview follow_up_update reaction analysis recap rumor correction denial opinion syndicated_copy unknown".split())
_PRECISIONS = frozenset("datetime date month quarter year unknown".split())
_PERIOD_KINDS = frozenset("fiscal_quarter fiscal_half fiscal_year calendar_quarter calendar_year date_range point_in_time unknown".split())
_PARTICIPANT_ROLES = frozenset("subject acquirer target buyer seller partner customer supplier issuer lender investor analyst regulator government plaintiff defendant trial_sponsor other".split())
LEGACY_ALIASES = {
    "earnings": ("earnings_results", None), "geopolitical": ("geopolitical_event", None),
    "military action": ("geopolitical_event", "military_action"),
    "military conflict": ("geopolitical_event", "military_conflict"),
    "sanctions": ("fiscal_trade_policy", "sanctions"),
    "retail event": ("retail_sales_promotion_event", "other_sales_event"),
}


def normalize_token(value: Any, *, limit: int = 120) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())[:limit]
    return text or None


def normalize_event_identity(raw: dict[str, Any] | None, *, ticker: str, exchange_code: str,
                             occurred_at: datetime | None = None, legacy_event_type: str | None = None) -> dict[str, Any]:
    """Return a safe v1 identity.  No unknown field can make analysis parsing fail."""
    source = dict(raw or {})
    candidate = normalize_token(source.get("event_family"))
    if not candidate and legacy_event_type:
        candidate = normalize_token(legacy_event_type)
    family, alias_subtype = LEGACY_ALIASES.get(candidate or "", (candidate, None))
    classified = family in EVENT_FAMILIES
    subtype_candidate = normalize_token(source.get("event_subtype"))
    subtype = subtype_candidate if classified and subtype_candidate in SUBTYPES.get(family, frozenset()) else None
    subject = source.get("subject") if isinstance(source.get("subject"), dict) else {}
    period = source.get("period") if isinstance(source.get("period"), dict) else None
    participants = source.get("participants") if isinstance(source.get("participants"), list) else []
    safe_participants = []
    for participant in participants[:20]:
        if not isinstance(participant, dict):
            continue
        role = normalize_token(participant.get("role"))
        safe_participants.append({"role": role if role in _PARTICIPANT_ROLES else "other", "instrument_id": participant.get("instrument_id"), "name_raw": str(participant.get("name_raw") or "")[:240] or None})
    status = "classified" if classified else "unmapped"
    occurred_value = source.get("occurred_at") or (occurred_at.isoformat() if occurred_at else None)
    identity = {
        "schema_version": SCHEMA_VERSION, "taxonomy_version": TAXONOMY_VERSION,
        "classification_status": status, "event_family": family if classified else None,
        "event_family_candidate": None if classified else candidate,
        "event_subtype": subtype or (alias_subtype if classified else None),
        "event_subtype_candidate": subtype_candidate if classified and subtype is None and subtype_candidate else None,
        "event_stage": _controlled(source.get("event_stage"), _STAGES, "unknown"),
        "coverage_role": _controlled(source.get("coverage_role"), _ROLES, "unknown"),
        "subject": {"instrument_id": subject.get("instrument_id"), "ticker": str(subject.get("ticker") or ticker).upper(), "exchange_code": str(subject.get("exchange_code") or exchange_code).upper()},
        "participants": safe_participants, "period": _safe_period(period), "occurred_at": occurred_value,
        "occurred_at_precision": _controlled(source.get("occurred_at_precision"), _PRECISIONS, "unknown"),
        "identifiers": [str(value)[:240] for value in source.get("identifiers", [])[:20] if str(value).strip()],
        "attributes": source.get("attributes") if isinstance(source.get("attributes"), dict) else {},
        "confidence": _confidence(source.get("confidence")),
        "provenance": {"source": "full_analysis_llm", "prompt_version": "event-identity-v1", "raw_response": source},
    }
    identity["event_instance_key"] = _instance_key(identity)
    return identity


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
    return gaps


def _instance_key(identity: dict[str, Any]) -> str | None:
    if identity["classification_status"] != "classified": return None
    subject, family = identity["subject"], identity["event_family"]
    discriminator = identity.get("identifiers") or [identity.get("period"), identity.get("occurred_at")]
    if not subject.get("ticker") or not family or not any(discriminator): return None
    payload = json.dumps([subject.get("instrument_id") or subject["ticker"], family, identity.get("event_subtype"), discriminator], sort_keys=True, default=str)
    return "v1:" + hashlib.sha256(payload.encode()).hexdigest()[:32]

def _controlled(value: Any, allowed: frozenset[str], default: str) -> str: return normalize_token(value) if normalize_token(value) in allowed else default
def _safe_period(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value: return None
    return {"kind": _controlled(value.get("kind"), _PERIOD_KINDS, "unknown"), "fiscal_year": value.get("fiscal_year"), "fiscal_quarter": value.get("fiscal_quarter"), "label_raw": str(value.get("label_raw") or "")[:120] or None}
def _confidence(value: Any) -> float:
    try: return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError): return 0.0
def _path(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict): return None
        current = current.get(key)
    return current
