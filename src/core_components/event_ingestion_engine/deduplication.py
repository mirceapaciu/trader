"""Strong and soft deduplication logic for canonical events."""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from .canonicalization import normalize_canonical_locator
from .models import CanonicalEvent, DedupeDecision, SoftDedupePolicy


def _normalize_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return {token for token in normalized.split(" ") if token}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _score(candidate: CanonicalEvent, event: CanonicalEvent) -> tuple[float, float, float, float]:
    event_title_tokens = _normalize_tokens(event.title)
    candidate_title_tokens = _normalize_tokens(candidate.title)
    event_summary_tokens = _normalize_tokens(event.summary)
    candidate_summary_tokens = _normalize_tokens(candidate.summary)

    title_score = _jaccard(event_title_tokens, candidate_title_tokens)
    summary_score = _jaccard(event_summary_tokens, candidate_summary_tokens)

    event_locator = normalize_canonical_locator(event.canonical_locator)
    candidate_locator = normalize_canonical_locator(candidate.canonical_locator)
    locator_score = 1.0 if event_locator == candidate_locator else 0.0

    score = 0.70 * title_score + 0.20 * summary_score + 0.10 * locator_score
    return score, title_score, summary_score, locator_score


def evaluate_dedupe(
    event: CanonicalEvent,
    persisted_candidates: list[CanonicalEvent],
    policy: SoftDedupePolicy,
) -> DedupeDecision:
    """Apply required strong and soft dedupe contracts for one canonical event."""
    ordered = sorted(persisted_candidates, key=lambda value: (value.occurred_at, value.id))

    # Strong dedupe by deterministic canonical identity.
    for candidate in ordered:
        if candidate.id == event.id:
            return DedupeDecision(
                accepted=False,
                reason_code="rejected_strong_duplicate",
                matched_event_id=candidate.id,
            )

    if not policy.enabled:
        return DedupeDecision(accepted=True, reason_code="accepted_unique")

    survivors: list[tuple[CanonicalEvent, float, float, float, float]] = []
    max_delta = timedelta(hours=policy.max_time_delta_hours)
    event_title_tokens = _normalize_tokens(event.title)

    lookback_start = event.occurred_at - policy.lookback_window
    lookback_end = event.occurred_at

    for candidate in ordered:
        if candidate.source != event.source:
            continue
        if candidate.occurred_at < lookback_start or candidate.occurred_at > lookback_end:
            continue

        if abs(event.occurred_at - candidate.occurred_at) > max_delta:
            continue

        candidate_title_tokens = _normalize_tokens(candidate.title)
        if len(event_title_tokens & candidate_title_tokens) == 0:
            continue

        score, title_score, summary_score, locator_score = _score(candidate, event)
        survivors.append((candidate, score, title_score, summary_score, locator_score))

    if not survivors:
        return DedupeDecision(
            accepted=True,
            reason_code="accepted_unique",
            algorithm_version=policy.algorithm_version,
            audit={
                "algorithm": policy.algorithm,
                "algorithm_version": policy.algorithm_version,
                "threshold": policy.threshold,
                "lookback_window_seconds": int(policy.lookback_window.total_seconds()),
                "max_time_delta_hours": policy.max_time_delta_hours,
            },
        )

    # Deterministic winner selection and tie-break order per contract.
    event_locator = normalize_canonical_locator(event.canonical_locator)
    ranked = sorted(
        survivors,
        key=lambda item: (
            -item[1],
            -(
                1
                if normalize_canonical_locator(item[0].canonical_locator) == event_locator
                else 0
            ),
            -item[2],
            item[0].occurred_at,
            item[0].id,
        ),
    )

    candidate, score, title_score, summary_score, locator_score = ranked[0]
    rejected = score >= policy.threshold
    return DedupeDecision(
        accepted=not rejected,
        reason_code="rejected_soft_duplicate" if rejected else "accepted_unique",
        matched_event_id=candidate.id,
        similarity_score=score,
        algorithm_version=policy.algorithm_version,
        audit={
            "algorithm": policy.algorithm,
            "algorithm_version": policy.algorithm_version,
            "threshold": policy.threshold,
            "lookback_window_seconds": int(policy.lookback_window.total_seconds()),
            "max_time_delta_hours": policy.max_time_delta_hours,
            "matched_event_id": candidate.id,
            "title_score": title_score,
            "summary_score": summary_score,
            "locator_score": locator_score,
            "score": score,
        },
    )
