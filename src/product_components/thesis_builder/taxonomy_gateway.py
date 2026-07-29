"""ThesisBuilder-owned asynchronous command surface for taxonomy decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol
import uuid

import redis

from .taxonomy_decisions import TaxonomyDecisionRequest

_PLACEHOLDER_ACTORS = frozenset({"", "configured-monitoring-operator"})


@dataclass(frozen=True)
class TaxonomyBackfillStatus:
    job_id: int
    status: str
    requested_taxonomy_revision: int
    target_taxonomy_revision: int
    last_analysis_id: int
    matched_count: int
    processed_count: int
    changed_count: int
    skipped_count: int
    failed_count: int
    retry_count: int
    error_code: str | None
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class TaxonomyCommand:
    command_id: str
    gap_id: int
    action: str
    status: str
    taxonomy_revision: int | None
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    backfill: TaxonomyBackfillStatus | None = None


class TaxonomyCommandRepository(Protocol):
    def submit_taxonomy_command(
        self,
        *,
        command_id: str,
        request: TaxonomyDecisionRequest,
        actor: str,
    ) -> tuple[TaxonomyCommand, bool]: ...

    def get_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand | None: ...

    def mark_taxonomy_command_publish_failed(self, *, command_id: str) -> None: ...


class TaxonomyCommandPublisher(Protocol):
    def publish_taxonomy_command(self, *, command_id: str) -> None: ...


class RedisTaxonomyCommandPublisher:
    def __init__(self, *, queue_url: str, command_stream: str) -> None:
        self._client = redis.from_url(queue_url, decode_responses=True)
        self._command_stream = command_stream

    def publish_taxonomy_command(self, *, command_id: str) -> None:
        self._client.xadd(self._command_stream, {"command_id": command_id})


class ThesisTaxonomyDecisionGateway:
    """Persist an accepted command before publishing it to ThesisBuilder."""

    def __init__(
        self,
        *,
        repository: TaxonomyCommandRepository,
        command_publisher: TaxonomyCommandPublisher,
        command_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._repository = repository
        self._command_publisher = command_publisher
        self._command_id_factory = command_id_factory

    def submit(
        self,
        *,
        request: TaxonomyDecisionRequest,
        actor: str,
    ) -> TaxonomyCommand:
        trusted_actor = actor.strip()
        if trusted_actor in _PLACEHOLDER_ACTORS or len(trusted_actor) > 200:
            raise PermissionError("trusted_taxonomy_actor_required")
        command, created = self._repository.submit_taxonomy_command(
            command_id=self._command_id_factory(),
            request=request,
            actor=trusted_actor,
        )
        if not created:
            return command
        try:
            self._command_publisher.publish_taxonomy_command(command_id=command.command_id)
        except Exception:
            self._repository.mark_taxonomy_command_publish_failed(command_id=command.command_id)
            # The accepted database row is the source of truth. ThesisBuilder's
            # recovery poll will execute it even if Redis is temporarily down.
        return command

    def get(self, *, command_id: str) -> TaxonomyCommand | None:
        return self._repository.get_taxonomy_command(command_id=command_id)
