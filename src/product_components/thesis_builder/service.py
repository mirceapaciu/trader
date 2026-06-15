from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from src.core_components.event_ingestion_engine.errors import TransientPublishError
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentRegistry,
    PostgresSharedThesisCardReviewWriter,
    SharedInstrumentRecord,
)

from .llm_client import OpenAIThesisClient, ThesisAnalyzer
from .models import InstrumentIdentity, NewsArticle
from .redis_io import NewsStreamMessage, RedisThesisBuilderIo
from .repository import PostgresThesisBuilderRepository
from .settings import ThesisBuilderSettings

LOGGER = logging.getLogger("thesis_builder.service")


class MarketContextClient(Protocol):
    def get_market_context(self, *, ticker: str, exchange_code: str, refresh_if_stale: bool = True):
        """Return a MarketData context snapshot or None."""


class InstrumentRegistry(Protocol):
    def list_active_instruments(self) -> list[SharedInstrumentRecord]:
        """Return active instrument registry rows with aliases."""


class ThesisCardReviewWriter(Protocol):
    def upsert_system_approved_review(self, *, card_id: str, reviewed_at: datetime, review_reason: str = "thesis_builder_preapproved_v1") -> None:
        """Persist preapproved shared review state."""


@dataclass(frozen=True)
class ThesisBuilderRuntimeStatus:
    news_raw_length: int | None
    pending_count: int | None
    signal_length: int | None
    processing_enabled: bool


@dataclass(frozen=True)
class ProcessMessageResult:
    acked: bool
    analyses_created: int
    signals_published: int


class ThesisBuilderRunner:
    """Operational ThesisBuilder Redis consumer."""

    def __init__(
        self,
        *,
        settings: ThesisBuilderSettings,
        repository: PostgresThesisBuilderRepository | None = None,
        redis_io: RedisThesisBuilderIo | None = None,
        analyzer: ThesisAnalyzer | None = None,
        market_context_client: MarketContextClient | None = None,
        instrument_registry: InstrumentRegistry | None = None,
        review_writer: ThesisCardReviewWriter | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository or PostgresThesisBuilderRepository(
            dsn=settings.postgres_dsn,
            thesis_schema=settings.thesis_builder_db_schema,
        )
        self._redis = redis_io or RedisThesisBuilderIo(
            queue_url=settings.queue_url,
            news_raw_queue=settings.news_raw_queue,
            signal_queue=settings.signal_queue,
            failed_messages_dlq=settings.failed_messages_dlq,
            consumer_group=settings.consumer_group,
            consumer_name=settings.consumer_name,
        )
        self._analyzer = analyzer or ThesisAnalyzer(
            client=OpenAIThesisClient(),
            model=settings.llm_model,
            max_tokens_per_run=settings.llm_daily_token_budget,
            max_tokens_per_item=settings.llm_max_output_tokens,
        )
        self._market_context_client = market_context_client
        self._instrument_registry = instrument_registry or PostgresSharedInstrumentRegistry(
            dsn=settings.postgres_dsn,
            shared_schema=settings.shared_db_schema,
            watchlist_table="t_watchlist_tickers",
        )
        self._review_writer = review_writer or PostgresSharedThesisCardReviewWriter(
            dsn=settings.postgres_dsn,
            shared_schema=settings.shared_db_schema,
        )

    def run_forever(self) -> None:
        self.bootstrap()
        LOGGER.info("ThesisBuilder runtime started")
        last_heartbeat = 0.0
        while True:
            try:
                processed = self.run_once()
                now = time.monotonic()
                if processed == 0 and now - last_heartbeat >= self._settings.heartbeat_interval_seconds:
                    self._log_heartbeat()
                    last_heartbeat = now
            except Exception:
                LOGGER.exception("Top-level ThesisBuilder cycle failure")
                time.sleep(max(1, self._settings.poll_interval_seconds))

    def bootstrap(self) -> None:
        self._redis.ping()
        self._redis.ensure_streams_and_group()

    def run_once(self) -> int:
        messages = self._redis.read(
            count=max(1, self._settings.batch_size),
            block_ms=max(1, self._settings.block_ms),
        )
        for message in messages:
            self._process_with_retry(message)
        return len(messages)

    def status(self) -> ThesisBuilderRuntimeStatus:
        news_raw_length, signal_length = self._redis.stream_lengths()
        return ThesisBuilderRuntimeStatus(
            news_raw_length=news_raw_length,
            pending_count=self._redis.pending_count(),
            signal_length=signal_length,
            processing_enabled=True,
        )

    def _process_with_retry(self, message: NewsStreamMessage) -> None:
        try:
            result = self.process_message(message)
            LOGGER.info(
                "Processed ThesisBuilder message id=%s article_id=%s acked=%s analyses=%s signals=%s",
                message.message_id,
                message.article_id,
                result.acked,
                result.analyses_created,
                result.signals_published,
            )
        except Exception as exc:
            delivery_count = self._redis.delivery_count(message.message_id)
            error_code = exc.__class__.__name__
            if delivery_count >= self._settings.max_delivery_attempts:
                LOGGER.exception(
                    "ThesisBuilder message failed permanently id=%s article_id=%s delivery_count=%s",
                    message.message_id,
                    message.article_id,
                    delivery_count,
                )
                self._redis.publish_dlq(message=message, error_code=error_code)
                self._redis.ack(message.message_id)
                return
            LOGGER.exception(
                "ThesisBuilder message failed and will remain pending id=%s article_id=%s delivery_count=%s",
                message.message_id,
                message.article_id,
                delivery_count,
            )

    def process_message(self, message: NewsStreamMessage) -> ProcessMessageResult:
        article = message.as_article()
        if article is None:
            self._redis.publish_dlq(message=message, error_code="missing_article_payload")
            self._redis.ack(message.message_id)
            return ProcessMessageResult(acked=True, analyses_created=0, signals_published=0)

        instruments = _resolve_instruments(
            article=article,
            active_instruments=self._instrument_registry.list_active_instruments(),
        )
        if not instruments:
            self._redis.ack(message.message_id)
            return ProcessMessageResult(acked=True, analyses_created=0, signals_published=0)

        analyses_created = 0
        signals_published = 0
        for instrument in instruments:
            context_snapshot = self._load_market_context(instrument)
            try:
                analysis = self._analyzer.analyze_article(
                    article=article,
                    ticker=instrument.ticker,
                    exchange_code=instrument.exchange_code,
                    market_context_snapshot=context_snapshot,
                )
            except ValueError as exc:
                self._repository.persist_rejected_analysis(
                    article=article,
                    instrument=instrument,
                    rejection_reason_code=str(exc) or "invalid_llm_response",
                    llm_model=self._settings.llm_model,
                    validation_errors=[str(exc) or "invalid_llm_response"],
                )
                analyses_created += 1
                continue

            result = self._repository.persist_analysis_and_update_evidence(
                article=article,
                result=analysis,
                market_context_snapshot=context_snapshot,
                required_evidence_count=self._settings.required_evidence_count,
                min_confidence=self._settings.min_confidence,
                risk_max_loss_usd=self._settings.risk_max_loss_usd,
                default_time_horizon=self._settings.default_time_horizon,
                evidence_collection_max_minutes=self._settings.evidence_collection_max_minutes,
                max_evidence_age_minutes=self._settings.max_evidence_age_minutes,
            )
            analyses_created += 1
            if result.signal is not None:
                self._review_writer.upsert_system_approved_review(
                    card_id=result.signal.thesis_card_id,
                    reviewed_at=result.signal.created_at,
                )
                self._redis.publish_signal(result.signal.envelope())
                self._repository.mark_signal_published(
                    result.signal.thesis_card_id,
                    published_at=datetime.now(timezone.utc),
                )
                signals_published += 1

        self._redis.ack(message.message_id)
        return ProcessMessageResult(
            acked=True,
            analyses_created=analyses_created,
            signals_published=signals_published,
        )

    def _load_market_context(self, instrument: InstrumentIdentity) -> dict[str, Any] | None:
        if self._market_context_client is None:
            return None
        snapshot = self._market_context_client.get_market_context(
            ticker=instrument.ticker,
            exchange_code=instrument.exchange_code,
            refresh_if_stale=True,
        )
        if snapshot is None:
            return None
        return _json_ready(asdict(snapshot))

    def _log_heartbeat(self) -> None:
        status = self.status()
        LOGGER.info(
            "ThesisBuilder heartbeat news_raw_length=%s pending_count=%s signal_length=%s processing_enabled=%s",
            status.news_raw_length,
            status.pending_count,
            status.signal_length,
            status.processing_enabled,
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _resolve_instruments(
    *,
    article: NewsArticle,
    active_instruments: list[SharedInstrumentRecord],
) -> list[InstrumentIdentity]:
    article_tickers = {ticker.strip().upper() for ticker in article.tickers if ticker.strip()}
    text = " ".join([article.headline, article.summary or "", article.url]).lower()
    matches: list[InstrumentIdentity] = []
    for instrument in active_instruments:
        if instrument.ticker in article_tickers or any(alias and alias in text for alias in instrument.aliases):
            matches.append(
                InstrumentIdentity(
                    ticker=instrument.ticker,
                    exchange_code=instrument.exchange_code,
                )
            )
    return matches
