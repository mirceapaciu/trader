from __future__ import annotations

import uuid
from typing import Callable, Protocol

import redis

from .repository import (
    PostgresThesisBuilderRepository,
    ReprocessRunAlreadyActive,
    ReprocessRunRecord,
)

__all__ = [
    "ReprocessCommandPublisher",
    "RedisReprocessCommandPublisher",
    "ReprocessRunAlreadyActive",
    "ReprocessRunRecord",
    "ThesisReprocessGateway",
]


class ReprocessCommandPublisher(Protocol):
    def publish_reprocess_command(self, *, run_id: str, days_back: int) -> None: ...


class RedisReprocessCommandPublisher:
    """Lightweight Redis publisher for reprocess commands.

    Owned by ThesisBuilder; used by callers (e.g. Monitoring UI) that only need
    to enqueue a reprocess command without joining the consumer group.
    """

    def __init__(self, *, queue_url: str, command_stream: str) -> None:
        self._client = redis.from_url(queue_url, decode_responses=True)
        self._command_stream = command_stream

    def publish_reprocess_command(self, *, run_id: str, days_back: int) -> None:
        self._client.xadd(
            self._command_stream,
            {"run_id": run_id, "days_back": str(days_back)},
        )


class ThesisReprocessGateway:
    """ThesisBuilder-owned surface for requesting and inspecting reprocess runs.

    Callers request a reprocess by enqueuing a command on the ThesisBuilder
    command stream and recording an 'accepted' run row. The ThesisBuilder
    runtime consumes the command and executes the reprocess in a background
    thread, updating the same run row to running/completed/failed.
    """

    def __init__(
        self,
        *,
        repository: PostgresThesisBuilderRepository,
        command_publisher: ReprocessCommandPublisher,
        run_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._repository = repository
        self._command_publisher = command_publisher
        self._run_id_factory = run_id_factory

    def request_reprocess(self, *, days_back: int) -> ReprocessRunRecord:
        run_id = self._run_id_factory()
        # The partial unique index uq_reprocess_runs_active is the hard
        # concurrency guard: this raises ReprocessRunAlreadyActive if another
        # accepted/running run exists.
        self._repository.insert_reprocess_run(run_id=run_id, days_back=days_back)
        try:
            self._command_publisher.publish_reprocess_command(run_id=run_id, days_back=days_back)
        except Exception:
            self._repository.mark_reprocess_failed(run_id=run_id, error_code="command_publish_failed")
            raise
        run = self._repository.get_reprocess_run(run_id=run_id)
        assert run is not None  # just inserted
        return run

    def get_run(self, *, run_id: str) -> ReprocessRunRecord | None:
        return self._repository.get_reprocess_run(run_id=run_id)
