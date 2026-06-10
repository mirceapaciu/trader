from __future__ import annotations

from collections import Counter, defaultdict

from .models import (
    ClassificationLabel,
    EvaluationScope,
    ItemAssessment,
    ItemStatus,
    RunSummary,
    quantize_rate,
)


def build_run_summary(
    *,
    dataset_input_count: int,
    dataset_rejected_count: int,
    dataset_accepted_count: int,
    accepted_audit_enabled: bool,
    accepted_audit_sample_size: int | None,
    evaluation_subject: str,
    assessments: list[ItemAssessment],
) -> RunSummary:
    evaluated = [item for item in assessments if item.item_status == ItemStatus.EVALUATED]
    rejected = [item for item in evaluated if item.evaluation_scope == EvaluationScope.REJECTED_POPULATION]
    accepted = [item for item in evaluated if item.evaluation_scope == EvaluationScope.ACCEPTED_AUDIT]

    correctly_rejected = _count_label(rejected, ClassificationLabel.CORRECTLY_REJECTED)
    incorrectly_rejected = _count_label(rejected, ClassificationLabel.INCORRECTLY_REJECTED)
    correctly_accepted = _count_label(accepted, ClassificationLabel.CORRECTLY_ACCEPTED)
    incorrectly_accepted = _count_label(accepted, ClassificationLabel.INCORRECTLY_ACCEPTED)

    rejected_items_evaluated = len(rejected)
    accepted_items_sampled = len(accepted)
    _validate_counts(
        rejected_items_evaluated=rejected_items_evaluated,
        correctly_rejected=correctly_rejected,
        incorrectly_rejected=incorrectly_rejected,
        accepted_items_sampled=accepted_items_sampled,
        correctly_accepted=correctly_accepted,
        incorrectly_accepted=incorrectly_accepted,
        dataset_rejected_count=dataset_rejected_count,
        dataset_accepted_count=dataset_accepted_count,
    )

    rejection_precision = quantize_rate(correctly_rejected, rejected_items_evaluated)
    incorrectly_accepted_rate = quantize_rate(incorrectly_accepted, accepted_items_sampled)
    accepted_coverage = quantize_rate(accepted_items_sampled, dataset_accepted_count)
    assumed_correct_accepted_count = dataset_accepted_count - accepted_items_sampled
    total_correct_count = correctly_rejected + correctly_accepted + assumed_correct_accepted_count
    total_filter_quality = quantize_rate(total_correct_count, dataset_input_count)
    error_drivers = _top_rejection_reason_error_drivers(rejected)
    grouped_recommendations = _grouped_recommendations(rejected)
    summary_json = {
        "rejection_precision_proxy": _decimal_or_none(rejection_precision),
        "top_rejection_reason_error_drivers": error_drivers,
        "grouped_recommendations": grouped_recommendations,
        "accepted_audit_enabled": accepted_audit_enabled,
        "accepted_audit_sample_size": accepted_audit_sample_size,
        "dataset_accepted_count": dataset_accepted_count,
        "accepted_items_sampled": accepted_items_sampled,
        "incorrectly_accepted_rate_estimate": _decimal_or_none(incorrectly_accepted_rate),
        "accepted_audit_coverage_ratio": _decimal_or_none(accepted_coverage),
        "accepted_audit_enabled_effective": bool(accepted_audit_enabled and accepted_items_sampled > 0),
        "assumed_correct_accepted_count": assumed_correct_accepted_count,
        "total_correct_count": total_correct_count,
        "total_filter_quality": _decimal_or_none(total_filter_quality),
        "evaluation_subject": evaluation_subject,
    }

    return RunSummary(
        dataset_input_count=dataset_input_count,
        dataset_rejected_count=dataset_rejected_count,
        dataset_accepted_count=dataset_accepted_count,
        rejected_items_evaluated=rejected_items_evaluated,
        accepted_items_sampled=accepted_items_sampled,
        correctly_rejected_count=correctly_rejected,
        incorrectly_rejected_count=incorrectly_rejected,
        correctly_accepted_count=correctly_accepted,
        incorrectly_accepted_count=incorrectly_accepted,
        rejection_precision_proxy=rejection_precision,
        incorrectly_accepted_rate_estimate=incorrectly_accepted_rate,
        total_filter_quality=total_filter_quality,
        total_correct_count=total_correct_count,
        assumed_correct_accepted_count=assumed_correct_accepted_count,
        evaluation_subject=evaluation_subject,
        summary_json=summary_json,
        recommendation_summary_md=_recommendation_markdown(
            rejection_precision=rejection_precision,
            incorrectly_accepted_rate=incorrectly_accepted_rate,
            error_drivers=error_drivers,
            grouped_recommendations=grouped_recommendations,
        ),
    )


def _validate_counts(
    *,
    rejected_items_evaluated: int,
    correctly_rejected: int,
    incorrectly_rejected: int,
    accepted_items_sampled: int,
    correctly_accepted: int,
    incorrectly_accepted: int,
    dataset_rejected_count: int,
    dataset_accepted_count: int,
) -> None:
    if rejected_items_evaluated != correctly_rejected + incorrectly_rejected:
        raise ValueError("invalid_rejected_count_consistency")
    if accepted_items_sampled != correctly_accepted + incorrectly_accepted:
        raise ValueError("invalid_accepted_count_consistency")
    if rejected_items_evaluated > dataset_rejected_count:
        raise ValueError("invalid_rejected_evaluation_count")
    if accepted_items_sampled > dataset_accepted_count:
        raise ValueError("invalid_accepted_sample_count")


def _count_label(items: list[ItemAssessment], label: ClassificationLabel) -> int:
    return sum(1 for item in items if item.classification_label == label)


def _top_rejection_reason_error_drivers(items: list[ItemAssessment]) -> list[dict[str, int | str]]:
    counter = Counter(
        item.rejection_reason_code or "unknown"
        for item in items
        if item.classification_label == ClassificationLabel.INCORRECTLY_REJECTED
    )
    return [
        {"rejection_reason_code": reason, "count": count}
        for reason, count in sorted(counter.items(), key=lambda entry: (-entry[1], entry[0]))
    ]


def _grouped_recommendations(items: list[ItemAssessment]) -> list[dict[str, object]]:
    grouped: dict[str, list[ItemAssessment]] = defaultdict(list)
    for item in items:
        if item.classification_label != ClassificationLabel.INCORRECTLY_REJECTED:
            continue
        grouped[str(item.probable_cause.value if item.probable_cause else "rule_conflict")].append(item)

    recommendations = []
    for cause, cause_items in sorted(grouped.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        suggestions = sorted(
            {
                item.improvement_suggestion
                for item in cause_items
                if item.improvement_suggestion
            }
        )
        recommendations.append(
            {
                "probable_cause": cause,
                "count": len(cause_items),
                "suggestions": suggestions[:5],
            }
        )
    return recommendations


def _recommendation_markdown(
    *,
    rejection_precision,
    incorrectly_accepted_rate,
    error_drivers,
    grouped_recommendations,
) -> str:
    lines = [
        "# Filter Quality Recommendations",
        "",
        f"- Rejection precision proxy: {_decimal_or_none(rejection_precision)}",
        f"- Incorrectly accepted rate estimate: {_decimal_or_none(incorrectly_accepted_rate)}",
    ]
    if error_drivers:
        lines.append("- Top rejection error drivers: " + ", ".join(
            f"{item['rejection_reason_code']} ({item['count']})" for item in error_drivers
        ))
    if grouped_recommendations:
        lines.append("- Grouped recommendations: " + ", ".join(
            f"{item['probable_cause']} ({item['count']})" for item in grouped_recommendations
        ))
    return "\n".join(lines)


def _decimal_or_none(value):
    return None if value is None else float(value)
