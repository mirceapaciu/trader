from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace

from src.product_components.shared.adapters import SharedInstrumentRecord
from src.product_components.thesis_builder.models import (
    ContentType,
    LlmAnalysisResult,
    LlmTriageResult,
    NewsArticle,
    ThesisStrategy,
    TradeDirection,
)
from src.product_components.thesis_builder.redis_io import NewsStreamMessage
from src.product_components.thesis_builder.service import ThesisBuilderRunner, _resolve_instruments
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
        self.rejected_kwargs: list[dict] = []
        self.processing_events: list[dict] = []

    def persist_rejected_analysis(self, **kwargs):
        self.rejected.append(kwargs["rejection_reason_code"])
        self.rejected_kwargs.append(kwargs)
        return 1

    def record_message_processing_event(self, **kwargs):
        self.processing_events.append(kwargs)


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
    repo = _FakeRepository()
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
        repository=repo,
        analyzer=_NoopAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(message)

    assert result.acked is True
    assert io.acked == ["1-0"]
    assert io.dlq == [("1-0", "missing_article_payload")]
    assert repo.processing_events == [
        {
            "source_message_id": "1-0",
            "event_id": "evt-1",
            "article_id": "missing",
            "outcome": "failed_dlq",
            "reason_code": "missing_article_payload",
            "analyses_created": 0,
            "signals_published": 0,
            "payload": {"id": "missing"},
        }
    ]


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
    repo = _FakeRepository()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=io,
        repository=repo,
        analyzer=_NoopAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id="article-1"))

    assert result.acked is True
    assert result.analyses_created == 0
    assert io.acked == ["1-0"]
    assert repo.processing_events[0]["outcome"] == "skipped"
    assert repo.processing_events[0]["reason_code"] == "no_active_instrument"


def test_triage_rejection_skips_full_analysis_and_persists_audit() -> None:
    article = _article()
    repo = _FakeRepository()
    io = _FakeIo()
    runner = ThesisBuilderRunner(
        settings=_settings(triage_enabled=True),
        redis_io=io,
        repository=repo,
        analyzer=_TriageRejectingAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(
            [SharedInstrumentRecord(ticker="AAPL", exchange_code="XNAS", aliases=("apple",))]
        ),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id=article.id))

    assert result.acked is True
    assert result.analyses_created == 1
    assert repo.rejected == ["triage_not_subject"]
    assert repo.rejected_kwargs[0]["triage_result"].reasoning == "list mention only"
    assert io.signals == []


def test_listicle_prefilter_persists_roundup_without_llm_call() -> None:
    repo = _FakeRepository()
    io = _FakeIo()
    runner = ThesisBuilderRunner(
        settings=_settings(
            listicle_prefilter_enabled=True,
            listicle_prefilter_tag_threshold=1,
        ),
        redis_io=io,
        repository=repo,
        analyzer=_FailingAnalyzer(),
        instrument_registry=_FakeInstrumentRegistry(
            [
                SharedInstrumentRecord(ticker="AAPL", exchange_code="XNAS", aliases=("apple",)),
                SharedInstrumentRecord(ticker="MSFT", exchange_code="XNAS", aliases=("microsoft",)),
            ]
        ),
        review_writer=_FakeReviewWriter(),
    )
    message = _message(article_id="roundup-1")
    message.payload["entities"] = ["AAPL", "MSFT"]
    message.payload["title"] = "Ten tech stocks investors are watching"

    result = runner.process_message(message)

    assert result.analyses_created == 2
    assert repo.rejected == ["prefiltered_roundup", "prefiltered_roundup"]
    assert repo.processing_events[0]["reason_code"] == "prefiltered_roundup"
    assert io.acked == ["1-0"]


def test_resolve_instruments_ignores_short_alias_inside_words() -> None:
    # Regression: the "mu" alias must not match inside "multi-trillion-dollar".
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        id="listicle-1",
        source="rss",
        headline="The Best Stocks to Invest $1,000 in Right Now",
        summary="Exploring some of the top bargains in a multi-trillion-dollar industry.",
        url="https://www.fool.com/investing/the-best-stocks-to-invest-1000-in-right-now",
        tickers=[],
        published_at=now,
        fetched_at=now,
    )
    instruments = _resolve_instruments(
        article=article,
        active_instruments=[
            SharedInstrumentRecord(
                ticker="MU",
                exchange_code="XNAS",
                aliases=("micron technology", "micron technology, inc.", "mu"),
            )
        ],
    )

    assert instruments == []


def test_resolve_instruments_matches_named_company() -> None:
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        id="micron-1",
        source="rss",
        headline="Micron Technology beats earnings estimates",
        summary="Strong memory demand.",
        url="https://example.com/micron",
        tickers=[],
        published_at=now,
        fetched_at=now,
    )
    instruments = _resolve_instruments(
        article=article,
        active_instruments=[
            SharedInstrumentRecord(
                ticker="MU",
                exchange_code="XNAS",
                aliases=("micron technology", "mu"),
            )
        ],
    )

    assert [i.ticker for i in instruments] == ["MU"]


def test_resolve_instruments_matches_google_press_name_alias() -> None:
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        id="goog-1",
        source="rss",
        headline="Google loses final E.U. appeal over Android fine",
        summary="Alphabet shares fell after the ruling.",
        url="https://example.com/google",
        tickers=[],
        published_at=now,
        fetched_at=now,
    )
    instruments = _resolve_instruments(
        article=article,
        active_instruments=[
            SharedInstrumentRecord(
                ticker="GOOGL",
                exchange_code="XNAS",
                aliases=("alphabet", "google", "googl"),
            )
        ],
    )

    assert [i.ticker for i in instruments] == ["GOOGL"]


def test_resolve_instruments_does_not_match_alias_only_in_url() -> None:
    now = datetime.now(timezone.utc)
    article = NewsArticle(
        id="url-1",
        source="rss",
        headline="Chip stocks move on broader demand hopes",
        summary="Memory makers rallied with the sector.",
        url="https://example.com/micron-technology-sector-roundup",
        tickers=[],
        published_at=now,
        fetched_at=now,
    )

    instruments = _resolve_instruments(
        article=article,
        active_instruments=[
            SharedInstrumentRecord(
                ticker="MU",
                exchange_code="XNAS",
                aliases=("micron technology", "mu"),
            )
        ],
    )

    assert instruments == []


def test_fundamentals_flow_to_analyzer_and_repository() -> None:
    article = _article()
    repo = _PersistingRepository()
    analyzer = _CapturingAnalyzer()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=_FakeIo(),
        repository=repo,
        analyzer=analyzer,
        market_context_client=_FakeMarketClient(),
        instrument_registry=_FakeInstrumentRegistry(
            [
                SharedInstrumentRecord(
                    ticker="AAPL",
                    exchange_code="XNAS",
                    aliases=("apple",),
                    display_name="Apple Inc.",
                )
            ]
        ),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id=article.id))

    assert result.analyses_created == 1
    snapshot = analyzer.kwargs["fundamentals_snapshot"]
    assert snapshot["market_cap_usd"] == 2.0e9
    # date -> isoformat, raw provider payload dropped from the audit copy.
    assert snapshot["next_earnings_date"] == "2026-07-30"
    assert "payload" not in snapshot
    # The exact prompt-input dict is persisted alongside the analysis.
    assert repo.persist_kwargs["fundamentals_snapshot"] == snapshot
    assert repo.persist_kwargs["instrument_display_name"] == "Apple Inc."
    assert repo.persist_kwargs["instrument_aliases"] == ("apple",)


def test_fundamentals_failure_never_blocks_analysis() -> None:
    article = _article()
    repo = _PersistingRepository()
    analyzer = _CapturingAnalyzer()
    runner = ThesisBuilderRunner(
        settings=_settings(),
        redis_io=_FakeIo(),
        repository=repo,
        analyzer=analyzer,
        market_context_client=_FakeMarketClient(fundamentals_error=True),
        instrument_registry=_FakeInstrumentRegistry(
            [SharedInstrumentRecord(ticker="AAPL", exchange_code="XNAS", aliases=("apple",))]
        ),
        review_writer=_FakeReviewWriter(),
    )

    result = runner.process_message(_message(article_id=article.id))

    assert result.analyses_created == 1
    assert analyzer.kwargs["fundamentals_snapshot"] is None
    assert repo.persist_kwargs["fundamentals_snapshot"] is None


class _FakeMarketClient:
    def __init__(self, *, fundamentals_error: bool = False) -> None:
        self._fundamentals_error = fundamentals_error

    def get_market_context(self, *, ticker, exchange_code, refresh_if_stale=True):
        return None

    def get_fundamentals(self, *, ticker, exchange_code, refresh_if_stale=True):
        if self._fundamentals_error:
            raise RuntimeError("finnhub down")
        return _FundamentalsSnapshot(
            ticker=ticker,
            market_cap_usd=2.0e9,
            next_earnings_date=date(2026, 7, 30),
            payload={"profile2": {"raw": True}},
        )


@dataclass(frozen=True)
class _FundamentalsSnapshot:
    ticker: str
    market_cap_usd: float
    next_earnings_date: date
    payload: dict


class _CapturingAnalyzer:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def analyze_article(self, **kwargs):
        self.kwargs = kwargs
        return LlmAnalysisResult(
            ticker="AAPL",
            exchange_code="XNAS",
            sentiment=0.5,
            relevance=0.9,
            urgency="today",
            suggested_action="buy",
            candidate_strategy=ThesisStrategy.EVENT_DRIVEN,
            direction=TradeDirection.BUY,
            confidence=0.8,
            reasoning="test",
            is_market_moving=True,
            instrument_is_subject=True,
            content_type=ContentType.NEWS_CATALYST,
        )


class _PersistingRepository(_FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.persist_kwargs: dict = {}

    def persist_analysis_and_update_evidence(self, **kwargs):
        self.persist_kwargs = kwargs
        return SimpleNamespace(analysis_id=1, signal=None)


class _NoopAnalyzer:
    pass


class _FailingAnalyzer:
    def analyze_article(self, **_kwargs):
        raise ValueError("invalid_test_response")


class _TriageRejectingAnalyzer:
    def triage_article(self, **_kwargs):
        return LlmTriageResult(
            ticker="AAPL",
            exchange_code="XNAS",
            instrument_is_subject=False,
            content_type=ContentType.NEWS_CATALYST,
            reasoning="list mention only",
            estimated_tokens=42,
            llm_model="triage-model",
        )

    def analyze_article(self, **_kwargs):
        raise AssertionError("full analysis should be skipped")


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
            "title": article.headline,
            "summary": article.summary,
            "canonical_locator": article.url,
            "entities": article.tickers,
            "occurred_at": article.published_at.isoformat(),
            "ingested_at": article.fetched_at.isoformat(),
            "attributes": {},
        },
        raw_fields={},
    )


def _settings(**overrides) -> ThesisBuilderSettings:
    values = dict(
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
        tradeability_max_entry_price=1000.0,
        tradeability_atr_stop_mult=1.5,
        default_time_horizon="swing_1d_5d",
        llm_model="test-model",
        llm_daily_token_budget=10000,
        llm_max_output_tokens=1200,
        triage_enabled=False,
        triage_model="test-triage-model",
        triage_max_output_tokens=200,
        story_scoping_enabled=False,
        story_assignment_model="test-story-model",
        story_assignment_max_output_tokens=120,
        synthesis_enabled=False,
        synthesis_model="test-synthesis-model",
        synthesis_max_output_tokens=1200,
        synthesis_fallback_to_mechanical=False,
        listicle_prefilter_enabled=False,
        listicle_prefilter_tag_threshold=6,
        already_priced_event_driven_atr_multiple=1.5,
        already_priced_event_driven_return_threshold=0.04,
        already_priced_sentiment_momentum_atr_multiple=2.0,
        already_priced_sentiment_momentum_return_threshold=0.06,
        llm_request_timeout_seconds=60.0,
        llm_max_retries=2,
        openai_api_key="test-key",
    )
    values.update(overrides)
    return ThesisBuilderSettings(
        **values,
    )
