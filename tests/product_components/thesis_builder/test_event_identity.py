from src.product_components.thesis_builder.event_identity import (
    TAXONOMY_VERSION,
    compare_event_identity,
    normalize_event_identity,
    taxonomy_gap_values,
)


def _identity(**overrides):
    raw = {"event_family": "earnings_results", "event_subtype": "quarterly_results", "period": {"kind": "fiscal_quarter", "fiscal_year": 2026, "fiscal_quarter": 2}}
    raw.update(overrides)
    return normalize_event_identity(raw, ticker="T", exchange_code="XNYS")


def test_legacy_alias_and_unknown_family_are_lossless() -> None:
    earnings = normalize_event_identity(None, ticker="TSM", exchange_code="XTAI", legacy_event_type="earnings")
    unknown = normalize_event_identity({"event_family": "space_elevator_failure"}, ticker="T", exchange_code="XNYS")

    assert earnings["event_family"] == "earnings_results"
    assert earnings["taxonomy_version"] == TAXONOMY_VERSION
    assert unknown["classification_status"] == "unmapped"
    assert unknown["event_family_candidate"] == "space_elevator_failure"
    assert taxonomy_gap_values(unknown) == [("event_family", "space_elevator_failure")]


def test_identity_comparison_distinguishes_periods_but_not_coverage_role() -> None:
    announcement = _identity(coverage_role="primary_announcement")
    reaction = _identity(coverage_role="reaction")
    next_quarter = _identity(period={"kind": "fiscal_quarter", "fiscal_year": 2026, "fiscal_quarter": 3})

    assert compare_event_identity(announcement, reaction) == "same"
    assert compare_event_identity(announcement, next_quarter) == "different"


def test_unknown_subtype_keeps_classified_family_and_records_gap() -> None:
    identity = _identity(event_subtype="surprise_release")
    assert identity["classification_status"] == "classified"
    assert identity["event_subtype"] is None
    assert taxonomy_gap_values(identity) == [("event_subtype", "surprise_release")]


def test_proposal_fields_and_unknown_secondary_values_are_lossless() -> None:
    identity = normalize_event_identity(
        {
            "event_family_candidate": "partnership",
            "event_stage": "announcement",
            "coverage_role": "breaking_news",
            "participants": [{"role": "counterparty", "name_raw": "Core Scientific"}],
        },
        ticker="AMD",
        exchange_code="XNAS",
    )

    assert identity["event_family_candidate"] == "partnership"
    assert identity["event_stage"] == "unknown"
    assert identity["event_stage_candidate"] == "announcement"
    assert identity["coverage_role_candidate"] == "breaking_news"
    assert identity["participants"][0]["role_candidate"] == "counterparty"
    assert taxonomy_gap_values(identity) == [
        ("event_family", "partnership"),
        ("event_stage", "announcement"),
        ("coverage_role", "breaking_news"),
        ("participant_role", "counterparty"),
    ]
