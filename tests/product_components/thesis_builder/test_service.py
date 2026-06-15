from datetime import datetime, timezone

from src.product_components.shared.adapters import SharedInstrumentRecord
from src.product_components.thesis_builder.models import NewsArticle
from src.product_components.thesis_builder.redis_io import NewsStreamMessage
from src.product_components.thesis_builder.service import ThesisBuilderRunner
from src.product_components.thesis_builder.settings import ThesisBuilderSettings


class _FakeIo:
    def __init__(self) -> None:
        self.bootstrapped = False
        self.acked: list[str] = []
        self.signals: list[dict] = []
        self.dlq: list[tuple[str, str]] = []
        self.messages: list[NewsStreamMessage] = []

    def ping(self) -> bool:
        return True

    def ensure_streams_and_group(self) -> None:
        self.bootstrapped = True

    def read(self, *, count: int, block_ms: int):
        return self.messages[:count]

    def ack(self, message_id: str) -> None:
        self.acked.append(message_id)

    def delivery_count(self, _message_id: str) -> int:
        return 1

    def publish_signal(self, envelope: dict) -> None:
        self.signals.append(envelope)

    def publish_dlq(self, *, message: NewsStreamMessage, error_code: str) -> None:
        self.dlq.append((message.message_id, error_code))

    def stream_lengths(self):
        return 0, len(self.signals)

    def pending_count(self):
        return 0


class _FakeRepository:
    def __init__(self) -> None:
        self.rejected: list[str] = []

    def persist_rejected_analysis(self, **kwargs):
        self.rejected.append(kwargs["rejection_reason_code"])
        return 1


class _FakeInstrumentRegistry:
    def __init__(self, rows: list[SharedInstrumentRecord] | None = None) -> None:
        self.rows = rows or []

    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        return list(self.rows)


class _FakeReviewWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    def upsert_system_approved_review(
        self,
        *,
        card_id: str,
        reviewed_at: datetime,
        review_reason: str = "thesis_builder_preapproved_v1",
    ) -> None:
        self.calls.append((card_id, reviewed_at))


def test_thesis_builder_runner_bootstraps_io() -> None:
    io = _FakeIo()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=_FakeRepository(),
        analyzer=_NoopAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(),
        review_writer=_FakeReviewWriter(),
    )

    runner.bootstrap()

    assert io.bootstrapped is True


def test_missing_article_goes_to_dlq_and_acks() -> None:
    io = _FakeIo()
    message = NewsStreamMessage(
        message_id="1-0",
        event_id="evt-1",
        event_type="news.article.created",
        dedupe_key="missing",
        payload={"id": "missing"},
        raw_fields={},
    )
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=_FakeRepository(),
        analyzer=_NoopAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(message)

    assert result.acked is True
    assert io.acked == ["1-0"]
    assert io.dlq == [("1-0", "missing_article_payload")]


def test_invalid_llm_response_is_persisted_and_acked() -> None:
    article = _article()
    repo = _FakeRepository()
    io = _FakeIo()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=repo,
        analyzer=_FailingAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(
            [SharedInstrumentRecord(ticker="AAPL", exchange_code="XNAS", aliases=("apple",))]
        ),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id=article.id))

    assert result.acked is True
    assert result.analyses_created == 1
    assert repo.rejected == ["invalid_test_response"]
    assert io.acked == ["1-0"]


def test_article_without_registry_match_is_acked_without_processing() -> None:
    io = _FakeIo()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=_FakeRepository(),
        analyzer=_NoopAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id="article-1"))

    assert result.acked is True
    assert result.analyses_created == 0
    assert io.acked == ["1-0"]


class _NoopAnalyzer:
    pass


class _FailingAnalyzer:
    def analyze_article(self, **_kwargs):
        raise ValueError("invalid_test_response")


def _article() -> NewsArticle:
    now = datetime.now(timezone.utc)
    return NewsArticle(
        id="article-1",
        source="test",
        headline="Apple raises guidance",
        summary="Apple raised revenue guidance.",
        url="https://example.com/aapl",
        tickers=["AAPL"],
        published_at=now,
        fetched_at=now,
    )


def _message(*, article_id: str) -> NewsStreamMessage:
    article = _article()
    return NewsStreamMessage(
        message_id="1-0",
        event_id="evt-1",
        event_type="news.article.created",
        dedupe_key=article_id,
        payload={
            "id": article_id,
            "source": article.source,
            "headline": article.headline,
            "summary": article.summary,
            "url": article.url,
            "tickers": article.tickers,
            "published_at": article.published_at.isoformat(),
            "fetched_at": article.fetched_at.isoformat(),
        },
        raw_fields={},
    )


def _settings() -> ThesisBuilderSettings:
    return ThesisBuilderSettings(
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        news_fetcher_db_schema="news_fetcher",
        queue_url="redis://localhost:6379/0",
        news_raw_queue="news_raw_queue",
        signal_queue="signal_queue",
        failed_messages_dlq="failed_messages_dlq",
        consumer_group="thesis_builder_group",
        consumer_name="consumer",
        poll_interval_seconds=120,
        heartbeat_interval_seconds=60,
        batch_size=10,
        block_ms=5000,
        max_delivery_attempts=3,
        evidence_collection_max_minutes=120,
        max_evidence_age_minutes=180,
        required_evidence_count=3,
        min_confidence=0.6,
        contrarian_min_confidence=0.72,
        trend_follow_min_confidence=0.68,
        risk_max_loss_usd=120.0,
        default_time_horizon="swing_1d_5d",
        llm_model="test-model",
        llm_daily_token_budget=10000,
        llm_max_output_tokens=1200,
    )
