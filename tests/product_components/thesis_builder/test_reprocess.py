from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.product_components.thesis_builder import reprocessor as reprocessor_module
from src.product_components.thesis_builder.models import NewsArticle
from src.product_components.thesis_builder.redis_io import NewsStreamMessage, ReprocessCommandMessage
from src.product_components.thesis_builder.reprocess_gateway import (
    ReprocessRunAlreadyActive,
    ThesisReprocessGateway,
)
from src.product_components.thesis_builder.repository import ReprocessRunRecord
from src.product_components.thesis_builder.reprocessor import ThesisBuilderReprocessor
from src.product_components.thesis_builder.service import ThesisBuilderRunner
from src.product_components.thesis_builder.settings import ThesisBuilderSettings


# --------------------------------------------------------------------------- #
# Gateway tests
# --------------------------------------------------------------------------- #


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, int]] = []
        self.raise_on_publish = False

    def publish_reprocess_command(self, *, run_id: str, days_back: int) -> None:
        if self.raise_on_publish:
            raise RuntimeError("redis down")
        self.published.append((run_id, days_back))


class _FakeGatewayRepository:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, int]] = []
        self.failed: list[str] = []
        self.active = False

    def insert_reprocess_run(self, *, run_id: str, days_back: int) -> None:
        if self.active:
            raise ReprocessRunAlreadyActive("existing")
        self.active = True
        self.inserted.append((run_id, days_back))

    def mark_reprocess_failed(self, *, run_id: str, error_code: str) -> None:
        self.failed.append(error_code)

    def get_reprocess_run(self, *, run_id: str) -> ReprocessRunRecord:
        return ReprocessRunRecord(
            run_id=run_id,
            days_back=7,
            max_articles=None,
            status="accepted",
            articles_found=None,
            analyses_created=None,
            cards_created=None,
            error_code=None,
            requested_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
            started_at=None,
            finished_at=None,
        )


def test_gateway_request_inserts_run_and_publishes_command() -> None:
    repo = _FakeGatewayRepository()
    publisher = _FakePublisher()
    gateway = ThesisReprocessGateway(
        repository=repo,
        command_publisher=publisher,
        run_id_factory=lambda: "run-1",
    )

    run = gateway.request_reprocess(days_back=5)

    assert run.run_id == "run-1"
    assert run.status == "accepted"
    assert repo.inserted == [("run-1", 5)]
    assert publisher.published == [("run-1", 5)]


def test_gateway_request_raises_when_already_active() -> None:
    repo = _FakeGatewayRepository()
    repo.active = True
    gateway = ThesisReprocessGateway(
        repository=repo,
        command_publisher=_FakePublisher(),
        run_id_factory=lambda: "run-2",
    )

    with pytest.raises(ReprocessRunAlreadyActive):
        gateway.request_reprocess(days_back=5)


def test_gateway_marks_failed_when_publish_fails() -> None:
    repo = _FakeGatewayRepository()
    publisher = _FakePublisher()
    publisher.raise_on_publish = True
    gateway = ThesisReprocessGateway(
        repository=repo,
        command_publisher=publisher,
        run_id_factory=lambda: "run-3",
    )

    with pytest.raises(RuntimeError):
        gateway.request_reprocess(days_back=5)

    assert repo.failed == ["command_publish_failed"]


# --------------------------------------------------------------------------- #
# Runner background-consumer tests
# --------------------------------------------------------------------------- #


class _FakeIo:
    def __init__(self, commands: list[ReprocessCommandMessage] | None = None) -> None:
        self._commands = list(commands or [])
        self.reprocess_acked: list[str] = []
        self.news_messages: list[NewsStreamMessage] = []
        self.news_acked: list[str] = []

    # News stream surface
    def read(self, *, count: int, block_ms: int):
        messages = self.news_messages[:count]
        self.news_messages = self.news_messages[count:]
        return messages

    def ack(self, message_id: str) -> None:
        self.news_acked.append(message_id)

    def delivery_count(self, _message_id: str) -> int:
        return 1

    def publish_dlq(self, **_kwargs) -> None:
        return None

    # Reprocess command surface
    def read_reprocess_commands(self, *, count: int, block_ms: int):
        drained = self._commands[:count]
        self._commands = self._commands[count:]
        return drained

    def ack_reprocess(self, message_id: str) -> None:
        self.reprocess_acked.append(message_id)


class _RecordingRepository:
    def __init__(self) -> None:
        self.running: list[tuple[str, int]] = []
        self.completed: list[tuple[str, int, int, int]] = []
        self.failed: list[tuple[str, str]] = []

    def mark_reprocess_running(self, *, run_id: str, max_articles: int) -> None:
        self.running.append((run_id, max_articles))

    def mark_reprocess_completed(self, *, run_id, articles_found, analyses_created, cards_created) -> None:
        self.completed.append((run_id, articles_found, analyses_created, cards_created))

    def mark_reprocess_failed(self, *, run_id: str, error_code: str) -> None:
        self.failed.append((run_id, error_code))


class _Result:
    def __init__(self, *, articles_found, analyses_created, cards_created) -> None:
        self.articles_found = articles_found
        self.analyses_created = analyses_created
        self.cards_created = cards_created


class _FakeReprocessor:
    def __init__(self, *, started: threading.Event | None = None, release: threading.Event | None = None) -> None:
        self.calls = 0
        self._started = started
        self._release = release

    def reprocess(self, *, days_back: int, max_articles: int) -> _Result:
        self.calls += 1
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            self._release.wait(timeout=5)
        return _Result(articles_found=10, analyses_created=4, cards_created=2)


def _runner(io: _FakeIo, repo: _RecordingRepository, reprocessor: _FakeReprocessor) -> ThesisBuilderRunner:
    return ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=repo,
        analyzer=object(),
        instrument_registry=object(),
        review_writer=object(),
        reprocessor_factory=lambda: reprocessor,
    )


def test_poll_reprocess_command_runs_and_records_completion() -> None:
    io = _FakeIo([ReprocessCommandMessage(message_id="c-1", run_id="run-1", days_back=5)])
    repo = _RecordingRepository()
    reprocessor = _FakeReprocessor()
    runner = _runner(io, repo, reprocessor)

    assert runner.poll_reprocess_commands() == 1
    runner._reprocess_thread.join(timeout=5)

    assert io.reprocess_acked == ["c-1"]
    assert repo.running == [("run-1", 200)]
    assert repo.completed == [("run-1", 10, 4, 2)]
    assert reprocessor.calls == 1


def test_concurrent_reprocess_command_is_skipped_while_active() -> None:
    started = threading.Event()
    release = threading.Event()
    io = _FakeIo([ReprocessCommandMessage(message_id="c-1", run_id="run-1", days_back=5)])
    repo = _RecordingRepository()
    reprocessor = _FakeReprocessor(started=started, release=release)
    runner = _runner(io, repo, reprocessor)

    runner.poll_reprocess_commands()
    assert started.wait(timeout=5)  # first run is in-flight

    # A second command arrives while the first is still running.
    io._commands = [ReprocessCommandMessage(message_id="c-2", run_id="run-2", days_back=5)]
    runner.poll_reprocess_commands()

    # Second command is acked but not executed.
    assert "c-2" in io.reprocess_acked
    release.set()
    runner._reprocess_thread.join(timeout=5)
    assert reprocessor.calls == 1
    assert repo.running == [("run-1", 200)]


def test_live_news_consumer_not_blocked_during_reprocess() -> None:
    started = threading.Event()
    release = threading.Event()
    io = _FakeIo([ReprocessCommandMessage(message_id="c-1", run_id="run-1", days_back=5)])
    io.news_messages = [
        NewsStreamMessage(
            message_id="n-1",
            event_id="evt-1",
            event_type="news.article.created",
            dedupe_key="missing",
            payload={"id": "missing"},  # no full article -> DLQ + ack, no LLM call
            raw_fields={},
        )
    ]
    repo = _RecordingRepository()
    reprocessor = _FakeReprocessor(started=started, release=release)
    runner = _runner(io, repo, reprocessor)

    runner.poll_reprocess_commands()
    assert started.wait(timeout=5)  # reprocess thread is blocking

    # The live consumer can still process a news message while reprocess runs.
    processed = runner.run_once()

    assert processed == 1
    assert io.news_acked == ["n-1"]

    release.set()
    runner._reprocess_thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Reprocessor article-selection tests
# --------------------------------------------------------------------------- #


class _StubAnalyzer:
    max_tokens_per_run = 10000
    model = "test-model"


def _article(idx: int) -> NewsArticle:
    ts = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc) + timedelta(hours=idx)
    return NewsArticle(
        id=f"a-{idx}",
        source="test",
        headline=f"headline {idx}",
        summary=None,
        url=f"https://example.com/{idx}",
        tickers=["AAA"],
        published_at=ts,
        fetched_at=ts,
    )


def test_reprocess_keeps_most_recent_articles_in_chronological_order(monkeypatch) -> None:
    # Five articles, oldest (idx 0) to newest (idx 4).
    articles = [_article(i) for i in range(5)]

    class _Registry:
        def list_active_instruments(self):
            return []

    reprocessor = ThesisBuilderReprocessor(
        dsn="postgresql://unused",
        news_fetcher_schema="news_fetcher",
        thesis_schema="thesis_builder",
        repository=object(),
        analyzer=_StubAnalyzer(),
        instrument_registry=_Registry(),
        required_evidence_count=3,
        min_confidence=0.6,
        risk_max_loss_usd=120.0,
        default_time_horizon="swing_1d_5d",
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
    )
    monkeypatch.setattr(reprocessor, "_fetch_articles", lambda *, days_back: list(articles))

    processed: list[str] = []

    def _record(*, article, active_instruments):
        processed.append(article.id)
        return []  # no instruments -> loop continues without analyzer/repo

    monkeypatch.setattr(reprocessor_module, "_resolve_instruments", _record)

    result = reprocessor.reprocess(days_back=7, max_articles=3)

    # The three NEWEST articles are processed, oldest-first (chronological).
    assert processed == ["a-2", "a-3", "a-4"]
    # articles_found reflects the full backlog, not the capped subset.
    assert result.articles_found == 5


def _settings() -> ThesisBuilderSettings:
    return ThesisBuilderSettings(
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        news_fetcher_db_schema="news_fetcher",
        queue_url="redis://localhost:6379/0",
        news_raw_queue="news_raw_queue",
        signal_queue="signal_queue",
        failed_messages_dlq="failed_messages_dlq",
        reprocess_command_queue="reprocess_command_queue",
        consumer_group="thesis_builder_group",
        consumer_name="consumer",
        reprocess_max_articles=200,
        poll_interval_seconds=120,
        heartbeat_interval_seconds=60,
        batch_size=10,
        block_ms=5000,
        claim_min_idle_seconds=300,
        max_delivery_attempts=3,
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
        required_evidence_count=3,
        min_confidence=0.6,
        min_relevance=0.5,
        contrarian_min_confidence=0.72,
        trend_follow_min_confidence=0.68,
        risk_max_loss_usd=120.0,
        default_time_horizon="swing_1d_5d",
        llm_model="test-model",
        llm_daily_token_budget=10000,
        llm_max_output_tokens=1200,
        triage_enabled=False,
        triage_model="test-triage-model",
        triage_max_output_tokens=200,
        listicle_prefilter_enabled=False,
        listicle_prefilter_tag_threshold=6,
        llm_request_timeout_seconds=60.0,
        llm_max_retries=2,
        openai_api_key="test-key",
    )
