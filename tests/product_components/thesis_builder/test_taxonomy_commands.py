from datetime import datetime, timezone

import pytest

from src.product_components.thesis_builder.taxonomy_decisions import TaxonomyDecisionRequest
from src.product_components.thesis_builder.taxonomy_gateway import (
    TaxonomyCommand,
    ThesisTaxonomyDecisionGateway,
)
from src.product_components.thesis_builder.taxonomy_worker import (
    TaxonomyBackfillAnalysis,
    TaxonomyBackfillJob,
    TaxonomyBackfillWorker,
    TaxonomyCommandRetryableError,
    TaxonomyCommandWorker,
)

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def _request() -> TaxonomyDecisionRequest:
    return TaxonomyDecisionRequest(
        gap_id=42,
        expected_gap_status="open",
        action="map_existing",
        canonical_value="partnership_joint_venture",
        display_name=None,
        description=None,
        family_scope=None,
        identity_discriminators=(),
        rationale="Established alias.",
        idempotency_key="gap-42-map",
    )


def _command(status: str = "accepted") -> TaxonomyCommand:
    return TaxonomyCommand(
        command_id="12345678-1234-1234-1234-123456789abc",
        gap_id=42,
        action="map_existing",
        status=status,
        taxonomy_revision=2 if status == "completed" else None,
        error_code=None,
        requested_at=NOW,
        started_at=NOW if status != "accepted" else None,
        finished_at=NOW if status in {"completed", "failed"} else None,
    )


class _GatewayRepository:
    def __init__(self, *, created: bool = True) -> None:
        self.created = created
        self.actor = None
        self.publish_failed = False

    def submit_taxonomy_command(self, *, command_id, request, actor):
        self.actor = actor
        return _command(), self.created

    def mark_taxonomy_command_publish_failed(self, *, command_id):
        self.publish_failed = True

    def get_taxonomy_command(self, *, command_id):
        return _command()


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.command_ids: list[str] = []

    def publish_taxonomy_command(self, *, command_id):
        self.command_ids.append(command_id)
        if self.fail:
            raise OSError("redis unavailable")


def test_gateway_requires_trusted_actor_and_returns_accepted_before_execution():
    repository = _GatewayRepository()
    publisher = _Publisher()
    gateway = ThesisTaxonomyDecisionGateway(
        repository=repository,
        command_publisher=publisher,
        command_id_factory=lambda: _command().command_id,
    )

    command = gateway.submit(request=_request(), actor="alice@example.test")

    assert command.status == "accepted"
    assert repository.actor == "alice@example.test"
    assert publisher.command_ids == [command.command_id]
    with pytest.raises(PermissionError, match="trusted_taxonomy_actor_required"):
        gateway.submit(request=_request(), actor="configured-monitoring-operator")


def test_gateway_keeps_durable_command_recoverable_when_publish_fails():
    repository = _GatewayRepository()
    gateway = ThesisTaxonomyDecisionGateway(
        repository=repository,
        command_publisher=_Publisher(fail=True),
    )

    assert gateway.submit(request=_request(), actor="alice").status == "accepted"
    assert repository.publish_failed is True


class _WorkerRepository:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def claim_taxonomy_command(self, *, command_id):
        return _command("running")

    def execute_taxonomy_command(self, *, command_id):
        self.executed.append(command_id)
        return _command("completed")

    def fail_taxonomy_command(self, *, command_id, error_code):
        self.failed.append((command_id, error_code))

    def recoverable_taxonomy_command_ids(self, *, limit):
        return ["one", "two"][:limit]


def test_command_worker_executes_recovered_commands_idempotently():
    repository = _WorkerRepository()
    worker = TaxonomyCommandWorker(repository=repository)

    assert worker.recover(limit=2) == 2
    assert repository.executed == ["one", "two"]
    assert repository.failed == []


def test_command_worker_leaves_post_decision_crash_recoverable():
    repository = _WorkerRepository()

    def crash_after_decision(*, command_id):
        raise TaxonomyCommandRetryableError("taxonomy_command_reconciliation_pending")

    repository.execute_taxonomy_command = crash_after_decision

    TaxonomyCommandWorker(repository=repository).process(command_id="one")

    assert repository.failed == []


class _BackfillRepository:
    def __init__(self) -> None:
        self.job = TaxonomyBackfillJob(
            job_id=7,
            decision_id=9,
            dimension="event_family",
            proposal="partnership",
            requested_taxonomy_revision=1,
            target_taxonomy_revision=2,
            last_analysis_id=0,
        )
        self.persisted = None
        self.failed = None

    def claim_taxonomy_backfill_job(self):
        job, self.job = self.job, None
        return job

    def get_taxonomy_backfill_batch(self, *, job, batch_size):
        assert batch_size == 2
        return [
            TaxonomyBackfillAnalysis(1, {"candidate": "partnership"}),
            TaxonomyBackfillAnalysis(2, {"candidate": "bad"}),
        ]

    def persist_taxonomy_backfill_batch(self, **kwargs):
        self.persisted = kwargs

    def fail_taxonomy_backfill_job(self, *, job_id, error_code):
        self.failed = (job_id, error_code)


def test_backfill_is_bounded_and_advances_past_a_malformed_row():
    repository = _BackfillRepository()

    def normalize(identity, revision):
        if identity["candidate"] == "bad":
            raise ValueError("malformed_identity")
        return {**identity, "event_family": "partnership_joint_venture", "taxonomy_revision": revision}

    worker = TaxonomyBackfillWorker(
        repository=repository,
        normalize=normalize,
        batch_size=2,
    )

    assert worker.run_batch() is True
    assert repository.failed is None
    assert repository.persisted["failed_count"] == 1
    assert [row[0] for row in repository.persisted["rows"]] == [1, 2]
    assert repository.persisted["complete"] is False


def test_backfill_batch_size_is_bounded():
    with pytest.raises(ValueError, match="taxonomy_backfill_batch_size_out_of_range"):
        TaxonomyBackfillWorker(
            repository=_BackfillRepository(),
            normalize=lambda identity, revision: identity,
            batch_size=1001,
        )
