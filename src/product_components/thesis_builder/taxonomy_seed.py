"""Immutable predefined data and bootstrap for event-taxonomy v1.

The manifest is deliberately separate from the revisioned runtime registry: it is
used only to create/repair revision-1 seed data.  Runtime reads the registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from psycopg.types.json import Json

from .taxonomy_runtime import EventTaxonomySnapshot, TaxonomyValue, build_taxonomy_snapshot

TAXONOMY_VERSION = "event-taxonomy-v1"


@dataclass(frozen=True)
class TaxonomySeedValue:
    dimension: str
    canonical_value: str
    display_name: str
    family_scope: str | None = None
    alias_for_value: str | None = None
    implied_subtype: str | None = None

    @property
    def status(self) -> str:
        return "mapped_alias" if self.alias_for_value else "active"


def _display(value: str) -> str:
    return value.replace("_", " ").title().replace(" And ", " and ")


_FAMILIES = """earnings_results guidance_outlook investor_day_strategy accounting_audit_restatement dividend share_repurchase financing_debt equity_offering credit_rating bankruptcy_restructuring merger_acquisition divestiture_spin_off ownership_stake_transaction partnership_joint_venture management_change governance_shareholder_action security_corporate_action listing_index_change commercial_contract_order government_award_subsidy capital_investment_capacity product_service_launch retail_sales_promotion_event product_pricing_change commercial_metrics production_operations_update supply_chain_disruption workforce_labor operational_incident_outage cybersecurity_incident product_recall_safety regulatory_approval regulatory_investigation_enforcement litigation_judgment_settlement intellectual_property filing_disclosure clinical_trial_result drug_device_regulatory investment_research_report analyst_rating_price_target digital_asset_event macroeconomic_data monetary_policy fiscal_trade_policy geopolitical_event political_election_transition natural_disaster_weather_event commodity_market_event sector_industry_development market_move""".split()
_SUBTYPES: Mapping[str, Sequence[str]] = MappingProxyType({
    "earnings_results": "quarterly_results half_year_results annual_results preliminary_results earnings_call".split(),
    "fiscal_trade_policy": "sanctions export_controls tariff subsidy tax_policy".split(),
    "geopolitical_event": "military_action military_conflict diplomatic_agreement ceasefire peace_deal".split(),
    "retail_sales_promotion_event": "prime_day black_friday cyber_monday other_sales_event".split(),
    "digital_asset_event": "protocol_upgrade stablecoin_authorization exchange_or_broker_launch token_regulatory_action digital_asset_market_event".split(),
    "investment_research_report": "short_seller_report activist_report independent_research".split(),
    "political_election_transition": "election government_formation leadership_transition referendum".split(),
    "natural_disaster_weather_event": "hurricane earthquake wildfire flood tornado extreme_weather".split(),
})
_DIMENSIONS = MappingProxyType({
    "event_stage": "proposed scheduled announced pending approved in_progress completed cancelled denied corrected unknown".split(),
    "coverage_role": "primary_announcement results_report preview follow_up_update reaction analysis recap rumor correction denial opinion syndicated_copy unknown".split(),
    "participant_role": "subject acquirer target buyer seller partner customer supplier issuer lender investor analyst regulator government plaintiff defendant trial_sponsor other".split(),
})
_ALIASES = (
    ("earnings", "earnings_results", None), ("geopolitical", "geopolitical_event", None),
    ("earnings_announcement", "earnings_results", None),
    ("earnings_report", "earnings_results", None),
    ("military action", "geopolitical_event", "military_action"),
    ("military conflict", "geopolitical_event", "military_conflict"),
    ("sanctions", "fiscal_trade_policy", "sanctions"),
    ("retail event", "retail_sales_promotion_event", "other_sales_event"),
)

PREDEFINED_TAXONOMY_V1 = tuple(
    [*(TaxonomySeedValue("event_family", value, _display(value)) for value in _FAMILIES),
     *(TaxonomySeedValue("event_subtype", value, _display(value), family_scope=family) for family, values in _SUBTYPES.items() for value in values),
     *(TaxonomySeedValue(dimension, value, _display(value)) for dimension, values in _DIMENSIONS.items() for value in values),
     *(TaxonomySeedValue("event_family", alias, _display(alias), alias_for_value=target, implied_subtype=implied) for alias, target, implied in _ALIASES)]
)


def validate_predefined_taxonomy() -> None:
    """Fail fast if an application release contains an invalid seed manifest."""
    canonical = [value for value in PREDEFINED_TAXONOMY_V1 if value.status == "active"]
    expected_counts = {"event_family": 50, "event_subtype": 37, "event_stage": 11, "coverage_role": 13, "participant_role": 18}
    actual_counts = {dimension: sum(value.dimension == dimension for value in canonical) for dimension in expected_counts}
    if actual_counts != expected_counts:
        raise RuntimeError("taxonomy_seed_canonical_counts_invalid")
    canonical_keys = {(value.dimension, value.canonical_value) for value in canonical}
    if len(canonical_keys) != len(canonical):
        raise RuntimeError("taxonomy_seed_duplicate_canonical_value")
    families = {value.canonical_value for value in canonical if value.dimension == "event_family"}
    subtypes = {value.canonical_value for value in canonical if value.dimension == "event_subtype"}
    for value in PREDEFINED_TAXONOMY_V1:
        if value.canonical_value != value.canonical_value.strip().lower() or not value.canonical_value:
            raise RuntimeError(f"taxonomy_seed_token_invalid:{value.canonical_value}")
        if value.dimension == "event_subtype" and value.family_scope not in families:
            raise RuntimeError(f"taxonomy_seed_subtype_scope_invalid:{value.canonical_value}")
        if value.status == "mapped_alias":
            if (value.dimension, value.alias_for_value) not in canonical_keys:
                raise RuntimeError(f"taxonomy_seed_alias_target_invalid:{value.canonical_value}")
            if value.implied_subtype and value.implied_subtype not in subtypes:
                raise RuntimeError(f"taxonomy_seed_implied_subtype_invalid:{value.canonical_value}")
            if value.implied_subtype:
                subtype_family = next(item.family_scope for item in canonical if item.dimension == "event_subtype" and item.canonical_value == value.implied_subtype)
                if subtype_family != value.alias_for_value:
                    raise RuntimeError(f"taxonomy_seed_implied_subtype_family_invalid:{value.canonical_value}")


def predefined_taxonomy_snapshot() -> EventTaxonomySnapshot:
    return build_taxonomy_snapshot(revision=1, values=predefined_taxonomy_values())


def predefined_taxonomy_values() -> tuple[TaxonomyValue, ...]:
    return tuple(
        TaxonomyValue(value.dimension, value.canonical_value, value.status, value.family_scope, value.alias_for_value, value.implied_subtype)
        for value in PREDEFINED_TAXONOMY_V1
    )


def bootstrap_predefined_taxonomy(connection, *, schema: str = "thesis_builder") -> None:
    """Insert missing manifest rows atomically; reject incompatible collisions."""
    validate_predefined_taxonomy()
    with connection.cursor() as cur:
        for value in PREDEFINED_TAXONOMY_V1:
            rules = {"family": value.family_scope} if value.family_scope else {}
            if value.implied_subtype:
                rules["implied_subtype"] = value.implied_subtype
            cur.execute(
                f"SELECT display_name, status, family_rules, alias_for_value, effective_from_revision, effective_to_revision FROM {schema}.t_event_taxonomy_values WHERE dimension = %s AND canonical_value = %s AND taxonomy_version = %s",
                (value.dimension, value.canonical_value, TAXONOMY_VERSION),
            )
            existing = cur.fetchone()
            expected = (value.display_name, value.status, rules, value.alias_for_value, 1, None)
            if existing is not None:
                actual = (existing[0], existing[1], existing[2] or {}, existing[3], existing[4], existing[5])
                if actual != expected:
                    raise RuntimeError(f"taxonomy_seed_collision:{value.dimension}:{value.canonical_value}")
                continue
            cur.execute(
                f"INSERT INTO {schema}.t_event_taxonomy_values (dimension, canonical_value, display_name, status, taxonomy_version, family_rules, alias_for_value, effective_from_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, 1)",
                (value.dimension, value.canonical_value, value.display_name, value.status, TAXONOMY_VERSION, Json(rules), value.alias_for_value),
            )
