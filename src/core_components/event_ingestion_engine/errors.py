"""Typed exceptions for ingestion and publication flows."""


class EventIngestionError(Exception):
    """Base class for engine-related errors."""


class TransientIngestionError(EventIngestionError):
    """A transient ingestion error that can be retried."""


class NonTransientIngestionError(EventIngestionError):
    """A non-transient ingestion error that must not be retried blindly."""


class PublicationError(EventIngestionError):
    """Base class for publication errors."""


class TransientPublishError(PublicationError):
    """A transient publish error that can be retried."""


class NonTransientPublishError(PublicationError):
    """A non-transient publish error that should be dead-lettered."""
