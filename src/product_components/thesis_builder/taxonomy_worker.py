"""Durable taxonomy command execution and bounded historical reclassification."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Protocol

from .taxonomy_gateway import TaxonomyCommand

LOGGER = logging.getLogger("thesis_builder.taxonomy_worker")


class TaxonomyCommandRetryableError(RuntimeError):
    """Decision may have committed; leave command recoverable for reconciliation."""


@dataclass(frozen=True)
class TaxonomyBackfillJob:
    job_id: int
    decision_id: int
    dimension: str
    proposal: str
    requested_taxonomy_revision: int
    target_taxonomy_revision: int
    last_analysis_id: int


@dataclass(frozen=True)
class TaxonomyBackfillAnalysis:
    analysis_id: int
    event_identity: dict[str, Any]


class TaxonomyWorkerRepository(Protocol):
    def claim_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand | None: ...
    def execute_taxonomy_command(self, *, command_id: str) -> TaxonomyCommand: ...
    def fail_taxonomy_command(self, *, command_id: str, error_code: str) -> None: ...
    def recoverable_taxonomy_command_ids(self, *, limit: int) -> list[str]: ...
    def claim_taxonomy_backfill_job(self) -> TaxonomyBackfillJob | None: ...
    def get_taxonomy_backfill_batch(
        self, *, job: TaxonomyBackfillJob, batch_size: int
    ) -> list[TaxonomyBackfillAnalysis]: ...
    def persist_taxonomy_backfill_batch(
        self,
        *,
        job: TaxonomyBackfillJob,
        rows: list[tuple[int, dict[str, Any], bool]],
        failed_count: int,
        complete: bool,
    ) -> None: ...
    def fail_taxonomy_backfill_job(self, *, job_id: int, error_code: str) -> None: ...


class TaxonomyCommandWorker:
    def __init__(self, *, repository: TaxonomyWorkerRepository) -> None:
        self._repository = repository

    def process(self, *, command_id: str) -> None:
        command = self._repository.claim_taxonomy_command(command_id=command_id)
        if command is None or command.status in {"completed", "failed"}:
            return
        try:
            completed = self._repository.execute_taxonomy_command(command_id=command_id)
            LOGGER.info(
                "Taxonomy command completed command_id=%s revision=%s",
                command_id,
                completed.taxonomy_revision,
            )
        except TaxonomyCommandRetryableError as exc:
            LOGGER.warning(
                "Taxonomy command awaiting reconciliation command_id=%s error_code=%s",
                command_id,
                _safe_error_code(exc),
            )
        except Exception as exc:
            error_code = _safe_error_code(exc)
            self._repository.fail_taxonomy_command(
                command_id=command_id, error_code=error_code
            )
            LOGGER.warning(
                "Taxonomy command failed command_id=%s error_code=%s",
                command_id,
                error_code,
            )

    def recover(self, *, limit: int = 10) -> int:
        command_ids = self._repository.recoverable_taxonomy_command_ids(limit=limit)
        for command_id in command_ids:
            self.process(command_id=command_id)
        return len(command_ids)


class TaxonomyBackfillWorker:
    def __init__(
        self,
        *,
        repository: TaxonomyWorkerRepository,
        normalize: Callable[[dict[str, Any], int], dict[str, Any]],
        batch_size: int,
    ) -> None:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("taxonomy_backfill_batch_size_out_of_range")
        self._repository = repository
        self._normalize = normalize
        self._batch_size = batch_size

    def run_batch(self) -> bool:
        job = self._repository.claim_taxonomy_backfill_job()
        if job is None:
            return False
        try:
            analyses = self._repository.get_taxonomy_backfill_batch(
                job=job, batch_size=self._batch_size
            )
            updates: list[tuple[int, dict[str, Any], bool]] = []
            failed = 0
            for analysis in analyses:
                try:
                    normalized = self._normalize(
                        analysis.event_identity, job.target_taxonomy_revision
                    )
                    updates.append(
                        (
                            analysis.analysis_id,
                            normalized,
                            normalized != analysis.event_identity,
                        )
                    )
                except Exception:
                    failed += 1
                    # Advance the durable cursor past a permanently malformed
                    # row while retaining its original immutable evidence.
                    updates.append(
                        (analysis.analysis_id, analysis.event_identity, False)
                    )
            self._repository.persist_taxonomy_backfill_batch(
                job=job,
                rows=updates,
                failed_count=failed,
                complete=len(analyses) < self._batch_size,
            )
            LOGGER.info(
                "Taxonomy backfill batch job_id=%s rows=%d failed=%d complete=%s",
                job.job_id,
                len(updates),
                failed,
                len(analyses) < self._batch_size,
            )
            return True
        except Exception as exc:
            error_code = _safe_error_code(exc)
            self._repository.fail_taxonomy_backfill_job(
                job_id=job.job_id, error_code=error_code
            )
            LOGGER.warning(
                "Taxonomy backfill failed job_id=%s error_code=%s",
                job.job_id,
                error_code,
            )
            return True


def _safe_error_code(exc: Exception) -> str:
    value = str(exc)
    if value and len(value) <= 80 and all(char.isalnum() or char in "_-" for char in value):
        return value
    return exc.__class__.__name__[:80]
