"""ThesisBuilder-owned validation for operator taxonomy decisions."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

DecisionAction = Literal["map_existing", "accept_new", "reject"]
_DIMENSIONS = frozenset({"event_family", "event_subtype", "event_stage", "coverage_role", "participant_role"})
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class TaxonomyDecisionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TaxonomyDecisionRequest:
    gap_id: int
    expected_gap_status: str
    action: DecisionAction
    canonical_value: str | None
    display_name: str | None
    description: str | None
    family_scope: str | None
    identity_discriminators: tuple[str, ...]
    rationale: str
    idempotency_key: str

    def validate(self, *, dimension: str) -> None:
        if self.gap_id <= 0 or self.expected_gap_status != "open":
            raise TaxonomyDecisionValidationError("taxonomy_gap_conflict")
        if dimension not in _DIMENSIONS:
            raise TaxonomyDecisionValidationError("unsupported_taxonomy_dimension")
        if not self.rationale.strip() or len(self.rationale.strip()) > 1000:
            raise TaxonomyDecisionValidationError("invalid_rationale")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:
            raise TaxonomyDecisionValidationError("invalid_idempotency_key")
        if self.action in {"map_existing", "accept_new"} and not _TOKEN.fullmatch(self.canonical_value or ""):
            raise TaxonomyDecisionValidationError("invalid_canonical_value")
        if self.action == "accept_new":
            if not self.display_name or len(self.display_name.strip()) > 120 or not self.description or len(self.description.strip()) > 1000:
                raise TaxonomyDecisionValidationError("invalid_new_canonical_value")
            if dimension == "event_subtype" and not _TOKEN.fullmatch(self.family_scope or ""):
                raise TaxonomyDecisionValidationError("family_scope_required")
