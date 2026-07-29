import pytest

from src.product_components.thesis_builder.taxonomy_decisions import (
    TaxonomyDecisionRequest,
    TaxonomyDecisionValidationError,
)


def _request(**changes):
    values = dict(
        gap_id=42, expected_gap_status="open", action="map_existing",
        canonical_value="partnership_joint_venture", display_name=None,
        description=None, family_scope=None, identity_discriminators=(),
        rationale="Established alias.", idempotency_key="operator-42-map",
    )
    values.update(changes)
    return TaxonomyDecisionRequest(**values)


def test_map_requires_open_gap_rationale_and_canonical_token():
    _request().validate(dimension="event_family")
    with pytest.raises(TaxonomyDecisionValidationError, match="invalid_rationale"):
        _request(rationale=" ").validate(dimension="event_family")
    with pytest.raises(TaxonomyDecisionValidationError, match="invalid_canonical_value"):
        _request(canonical_value="Partnership!").validate(dimension="event_family")


def test_accept_new_requires_operator_facing_definition():
    with pytest.raises(TaxonomyDecisionValidationError, match="invalid_new_canonical_value"):
        _request(action="accept_new", display_name="New value").validate(dimension="event_stage")
    _request(action="accept_new", canonical_value="pre_announcement", display_name="Pre-announcement", description="A verified announcement is expected.").validate(dimension="event_stage")


def test_subtype_acceptance_requires_canonical_family_scope():
    with pytest.raises(TaxonomyDecisionValidationError, match="family_scope_required"):
        _request(action="accept_new", display_name="New subtype", description="Definition.").validate(dimension="event_subtype")
    _request(action="accept_new", display_name="New subtype", description="Definition.", family_scope="partnership_joint_venture").validate(dimension="event_subtype")
