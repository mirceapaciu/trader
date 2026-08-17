from dataclasses import dataclass, field

import pytest

from src.product_components.thesis_builder.event_identity import (
    DEFAULT_TAXONOMY_SNAPSHOT,
    normalize_event_identity,
    renormalize_event_identity,
)
from src.product_components.thesis_builder.taxonomy_runtime import (
    EventTaxonomySnapshotProvider,
    TaxonomyValue,
    build_taxonomy_snapshot,
)
from src.product_components.thesis_builder.taxonomy_seed import predefined_taxonomy_values


@dataclass
class _Source:
    revision: int = 1
    values_by_revision: dict[int, tuple[TaxonomyValue, ...]] = field(
        default_factory=dict
    )
    fail: bool = False

    def get_taxonomy_revision(self) -> int:
        if self.fail:
            raise RuntimeError("database_unavailable")
        return self.revision

    def load_taxonomy_values(
        self, *, taxonomy_revision: int
    ) -> tuple[TaxonomyValue, ...]:
        if self.fail:
            raise RuntimeError("database_unavailable")
        return self.values_by_revision.get(taxonomy_revision, ())


def test_alias_is_effective_only_from_its_revision() -> None:
    source = _Source(
        revision=2,
        values_by_revision={
            2: predefined_taxonomy_values() + (
                TaxonomyValue(
                    dimension="event_family",
                    canonical_value="partnership",
                    status="mapped_alias",
                    alias_for_value="partnership_joint_venture",
                ),
            )
        },
    )
    provider = EventTaxonomySnapshotProvider(
        source=source, baseline=DEFAULT_TAXONOMY_SNAPSHOT
    )

    old_identity = normalize_event_identity(
        {"event_family": "partnership"},
        ticker="AMD",
        exchange_code="XNAS",
        taxonomy=provider.get(1),
    )
    new_identity = normalize_event_identity(
        {"event_family": "partnership"},
        ticker="AMD",
        exchange_code="XNAS",
        taxonomy=provider.get(2),
    )

    assert old_identity["classification_status"] == "unmapped"
    assert old_identity["event_family_candidate"] == "partnership"
    assert old_identity["taxonomy_revision"] == 1
    assert new_identity["event_family"] == "partnership_joint_venture"
    assert new_identity["event_family_candidate"] is None
    assert new_identity["provenance"]["taxonomy_revision"] == 2


def test_accepted_value_is_recognized_without_code_constant() -> None:
    snapshot = build_taxonomy_snapshot(
        revision=2,
        values=predefined_taxonomy_values() + (
            TaxonomyValue(
                dimension="event_family",
                canonical_value="space_industry_event",
                status="active",
            ),
        ),
    )

    identity = normalize_event_identity(
        {"event_family": "space_industry_event"},
        ticker="RKLB",
        exchange_code="XNAS",
        taxonomy=snapshot,
    )

    assert identity["classification_status"] == "classified"
    assert identity["event_family"] == "space_industry_event"


def test_subtype_scope_is_enforced() -> None:
    snapshot = build_taxonomy_snapshot(
        revision=2,
        values=predefined_taxonomy_values() + (
            TaxonomyValue(
                dimension="event_subtype",
                canonical_value="capacity_reservation",
                status="active",
                family_scope="commercial_contract_order",
            ),
        ),
    )

    valid = normalize_event_identity(
        {
            "event_family": "commercial_contract_order",
            "event_subtype": "capacity_reservation",
        },
        ticker="AMD",
        exchange_code="XNAS",
        taxonomy=snapshot,
    )
    wrong_family = normalize_event_identity(
        {
            "event_family": "partnership_joint_venture",
            "event_subtype": "capacity_reservation",
        },
        ticker="AMD",
        exchange_code="XNAS",
        taxonomy=snapshot,
    )

    assert valid["event_subtype"] == "capacity_reservation"
    assert wrong_family["event_subtype"] is None
    assert wrong_family["event_subtype_candidate"] == "capacity_reservation"


def test_latest_refresh_is_atomic_and_failure_safe() -> None:
    source = _Source()
    provider = EventTaxonomySnapshotProvider(
        source=source, baseline=DEFAULT_TAXONOMY_SNAPSHOT
    )
    in_flight = provider.get()

    source.revision = 2
    source.values_by_revision[2] = (
        TaxonomyValue(
            dimension="event_stage",
            canonical_value="paused",
            status="active",
        ),
    )
    refreshed = provider.refresh()

    assert in_flight.revision == 1
    assert in_flight.resolve("event_stage", "paused") is None
    assert refreshed.revision == 2
    assert refreshed.resolve("event_stage", "paused") == "paused"

    source.fail = True
    assert provider.get() is refreshed
    with pytest.raises(RuntimeError, match="database_unavailable"):
        provider.get(3)


def test_persisted_identity_can_be_renormalized_for_backfill() -> None:
    original = normalize_event_identity(
        {"event_family_candidate": "partnership"},
        ticker="AMD",
        exchange_code="XNAS",
    )
    snapshot = build_taxonomy_snapshot(
        revision=2,
        values=predefined_taxonomy_values() + (
            TaxonomyValue(
                dimension="event_family",
                canonical_value="partnership",
                status="mapped_alias",
                alias_for_value="partnership_joint_venture",
            ),
        ),
    )

    updated = renormalize_event_identity(original, taxonomy=snapshot)

    assert updated["event_family"] == "partnership_joint_venture"
    assert updated["taxonomy_revision"] == 2
    assert updated["subject"]["ticker"] == "AMD"


def test_database_rows_alone_preserve_implied_subtype_aliases() -> None:
    snapshot = build_taxonomy_snapshot(revision=1, values=predefined_taxonomy_values())

    identity = normalize_event_identity(
        {"event_family": "military action"}, ticker="LMT", exchange_code="XNYS", taxonomy=snapshot
    )

    assert identity["event_family"] == "geopolitical_event"
    assert identity["event_subtype"] == "military_action"


def test_later_deprecation_is_not_reintroduced_by_a_baseline() -> None:
    values = tuple(value for value in predefined_taxonomy_values() if value.canonical_value != "announced")
    snapshot = build_taxonomy_snapshot(revision=3, values=values, baseline=DEFAULT_TAXONOMY_SNAPSHOT)

    assert snapshot.resolve("event_stage", "announced") is None
