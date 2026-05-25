"""Real-time Event Ingestion and Signal Preprocessing Engine."""

from .canonicalization import canonicalize_source_event, generate_canonical_id
from .deduplication import evaluate_dedupe
from .engine import EventIngestionEngine, ProcessResult
from .errors import NonTransientPublishError, TransientPublishError
from .models import (
    CanonicalEvent,
    Checkpoint,
    DedupeDecision,
    FetchedBatch,
    PublicationObligation,
    PublicationStatus,
    SoftDedupePolicy,
    SourceEvent,
)

__all__ = [
    "CanonicalEvent",
    "Checkpoint",
    "DedupeDecision",
    "EventIngestionEngine",
    "FetchedBatch",
    "NonTransientPublishError",
    "ProcessResult",
    "PublicationObligation",
    "PublicationStatus",
    "SoftDedupePolicy",
    "SourceEvent",
    "TransientPublishError",
    "canonicalize_source_event",
    "evaluate_dedupe",
    "generate_canonical_id",
]
