from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.product_components.filter_quality_evaluator.models import (
    ClassificationLabel,
    EvaluationScope,
    ItemAssessment,
    ItemStatus,
    ProbableCause,
)
from src.product_components.filter_quality_evaluator.summary import build_run_summary


def _assessment(label: ClassificationLabel, *, reason: str = "rejected_not_relevant") -> ItemAssessment:
    return ItemAssessment(
        assessment_id=f"a-{label.value}-{reason}",
        run_id="run",
        article_id=f"article-{label.value}-{reason}",
        evaluation_scope=(
            EvaluationScope.ACCEPTED_AUDIT
            if "accepted" in label.value
            else EvaluationScope.REJECTED_POPULATION
        ),
        source="finnhub",
        published_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        filter_run_id_production="prod",
        filter_run_id_simulation="sim",
        production_filter_outcome="rejected",
        simulation_filter_outcome="rejected",
        is_disagreement=False,
        rejection_reason_code=reason,
        item_status=ItemStatus.EVALUATED,
        classification_label=label,
        classification_confidence=Decimal("0.9"),
        rationale="r",
        probable_cause=ProbableCause.KEYWORD_GAP,
        improvement_suggestion="add a keyword",
    )


def test_build_run_summary_applies_normative_rates_and_rankings() -> None:
    summary = build_run_summary(
        dataset_input_count=4,
        dataset_rejected_count=3,
        dataset_accepted_count=1,
        accepted_audit_enabled=True,
        accepted_audit_sample_size=1,
        assessments=[
            _assessment(ClassificationLabel.CORRECTLY_REJECTED),
            _assessment(ClassificationLabel.INCORRECTLY_REJECTED, reason="b_reason"),
            _assessment(ClassificationLabel.INCORRECTLY_REJECTED, reason="a_reason"),
            _assessment(ClassificationLabel.INCORRECTLY_ACCEPTED),
        ],
    )

    assert summary.rejection_precision_proxy == Decimal("0.33333")
    assert summary.incorrectly_accepted_rate_estimate == Decimal("1.00000")
    assert summary.summary_json["accepted_audit_coverage_ratio"] == 1.0
    assert summary.summary_json["top_rejection_reason_error_drivers"] == [
        {"rejection_reason_code": "a_reason", "count": 1},
        {"rejection_reason_code": "b_reason", "count": 1},
    ]


def test_build_run_summary_fails_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="invalid_rejected_evaluation_count"):
        build_run_summary(
            dataset_input_count=1,
            dataset_rejected_count=0,
            dataset_accepted_count=0,
            accepted_audit_enabled=False,
            accepted_audit_sample_size=None,
            assessments=[_assessment(ClassificationLabel.CORRECTLY_REJECTED)],
        )
