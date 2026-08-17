"""Revisioned runtime taxonomy snapshots for event identity normalization."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class TaxonomyValue:
    dimension: str
    canonical_value: str
    status: str
    family_scope: str | None = None
    alias_for_value: str | None = None
    implied_subtype: str | None = None


class TaxonomySnapshotSource(Protocol):
    def get_taxonomy_revision(self) -> int:
        """Return the latest committed taxonomy revision."""

    def load_taxonomy_values(self, *, taxonomy_revision: int) -> Sequence[TaxonomyValue]:
        """Return values effective at the requested revision."""


@dataclass(frozen=True)
class EventTaxonomySnapshot:
    revision: int
    canonical_values: Mapping[str, frozenset[str]]
    aliases: Mapping[str, Mapping[str, str]]
    subtype_families: Mapping[str, str]
    family_alias_subtypes: Mapping[str, str]

    def resolve(
        self,
        dimension: str,
        value: str | None,
        *,
        family_scope: str | None = None,
    ) -> str | None:
        if not value:
            return None
        resolved = self.aliases.get(dimension, {}).get(value, value)
        if resolved not in self.canonical_values.get(dimension, frozenset()):
            return None
        if dimension == "event_subtype":
            required_family = self.subtype_families.get(resolved)
            if required_family is not None and required_family != family_scope:
                return None
        return resolved


def build_taxonomy_snapshot(
    *,
    revision: int,
    values: Sequence[TaxonomyValue],
    baseline: EventTaxonomySnapshot | None = None,
) -> EventTaxonomySnapshot:
    """Build an immutable snapshot without performing I/O."""
    if revision <= 0:
        raise ValueError("invalid_taxonomy_revision")

    canonical: dict[str, set[str]] = {}
    aliases: dict[str, dict[str, str]] = {}
    subtype_families: dict[str, str] = {}
    family_alias_subtypes: dict[str, str] = {}
    # ``baseline`` remains an accepted argument for old callers, but is never
    # merged.  A database-loaded revision must be reconstructed solely from
    # rows effective at that revision.
    del baseline

    for value in values:
        dimension = value.dimension
        if value.status == "active":
            canonical.setdefault(dimension, set()).add(value.canonical_value)
            if dimension == "event_subtype" and value.family_scope:
                subtype_families[value.canonical_value] = value.family_scope
        elif value.status == "mapped_alias" and value.alias_for_value:
            aliases.setdefault(dimension, {})[value.canonical_value] = value.alias_for_value
            if dimension == "event_family" and value.implied_subtype:
                family_alias_subtypes[value.canonical_value] = value.implied_subtype

    return EventTaxonomySnapshot(
        revision=revision,
        canonical_values=MappingProxyType(
            {dimension: frozenset(items) for dimension, items in canonical.items()}
        ),
        aliases=MappingProxyType(
            {
                dimension: MappingProxyType(dict(items))
                for dimension, items in aliases.items()
            }
        ),
        subtype_families=MappingProxyType(subtype_families),
        family_alias_subtypes=MappingProxyType(family_alias_subtypes),
    )


class EventTaxonomySnapshotProvider:
    """Atomically caches immutable snapshots by integer revision."""

    def __init__(
        self,
        *,
        source: TaxonomySnapshotSource,
        baseline: EventTaxonomySnapshot | None = None,
    ) -> None:
        self._source = source
        self._lock = threading.RLock()
        self._snapshots: dict[int, EventTaxonomySnapshot] = {}
        self._last_valid: EventTaxonomySnapshot | None = None

    def get(self, taxonomy_revision: int | None = None) -> EventTaxonomySnapshot:
        """Return an explicit revision, or the latest failure-safe snapshot."""
        if taxonomy_revision is not None:
            return self._get_revision(taxonomy_revision)

        try:
            latest_revision = self._source.get_taxonomy_revision()
            return self._get_revision(latest_revision)
        except Exception:
            # A transient database failure must not swap a working taxonomy for
            # an empty or partially constructed snapshot.
            with self._lock:
                if self._last_valid is None:
                    raise
                return self._last_valid

    def refresh(self) -> EventTaxonomySnapshot:
        """Force a latest-revision check after a committed decision."""
        return self.get()

    def _get_revision(self, taxonomy_revision: int) -> EventTaxonomySnapshot:
        with self._lock:
            cached = self._snapshots.get(taxonomy_revision)
            if cached is not None:
                return cached

        values = self._source.load_taxonomy_values(
            taxonomy_revision=taxonomy_revision
        )
        snapshot = build_taxonomy_snapshot(
            revision=taxonomy_revision,
            values=values,
        )
        with self._lock:
            existing = self._snapshots.setdefault(taxonomy_revision, snapshot)
            if self._last_valid is None or existing.revision >= self._last_valid.revision:
                self._last_valid = existing
            return existing


def family_rules_scope(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    family = value.get("family")
    return str(family) if family else None


def family_rules_implied_subtype(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    subtype = value.get("implied_subtype")
    return str(subtype) if subtype else None
