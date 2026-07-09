from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from src.core_components.event_ingestion_engine.engine import EventIngestionEngine
from src.core_components.event_ingestion_engine.errors import NonTransientPublishError, TransientPublishError
from src.core_components.event_ingestion_engine.interfaces import EventPublisher, StorageAdapter
from src.core_components.event_ingestion_engine.models import ProcessResult, PublicationStatus, SoftDedupePolicy
from src.product_components.shared.adapters import PostgresSharedInstrumentRegistry

from .providers import (
    FinnhubCompanyNewsProvider,
    NewsProvider,
    ProviderRateLimitError,
    RssProvider,
    finnhub_company_news_source_key,
)
from .publisher import RedisStreamPublisher
from .settings import NewsFetcherSettings
from .source_adapter import NewsSourceAdapter, SourceFilterConfig
from .storage_adapter import PostgresNewsStorageAdapter

LOGGER = logging.getLogger(__name__)
_RETRY_DRAIN_LEASE_SECONDS = 60


class NewsFetcherService:
    """Coordinates one fetch->persist->publish cycle per configured provider."""

    def __init__(
        self,
        *,
        settings: NewsFetcherSettings,
        providers: dict[str, NewsProvider],
        storage: StorageAdapter,
        publisher: EventPublisher,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._storage = storage
        self._publisher = publisher
        self._source_backoff_until: dict[str, datetime] = {}
        self._source_interval_until: dict[str, datetime] = {}
        self._source_min_interval_seconds: dict[str, int] = {}
        self._retry_worker_id = f"news_fetcher_retry_{uuid.uuid4().hex}"

    def run_once(self) -> dict[str, ProcessResult]:
        results: dict[str, ProcessResult] = {}
        self._drain_retryable_obligations()
        watchlist = self._storage.load_active_watchlist_tickers()
        filter_config = self._production_filter_config(watchlist)
        providers = self._configured_providers()

        for source_key, provider in providers.items():
            started_at = datetime.now(timezone.utc)
            backoff_until = self._source_backoff_until.get(source_key)
            if self._is_backing_off(source_key, started_at):
                LOGGER.info(
                    "news_fetcher source fetch skipped source_key=%s reason=provider_rate_limit_backoff next_retry_at=%s",
                    source_key,
                    _isoformat(backoff_until),
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="provider_rate_limit_backoff",
                )
                continue
            interval_until = self._source_interval_until.get(source_key)
            if self._is_waiting_for_interval(source_key, started_at):
                LOGGER.info(
                    "news_fetcher source fetch skipped source_key=%s reason=source_interval_wait next_retry_at=%s",
                    source_key,
                    _isoformat(interval_until),
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="success",
                    error_code="source_interval_wait",
                )
                continue

            LOGGER.info(
                "news_fetcher source fetch attempt started source_key=%s attempt_at=%s",
                source_key,
                _isoformat(started_at),
            )
            adapter = NewsSourceAdapter(
                provider=provider,
                timeout_seconds=self._settings.provider_timeout_seconds,
                filter_config=SourceFilterConfig(
                    watchlist_tickers=set(filter_config.watchlist_tickers),
                    include_keywords=filter_config.include_keywords,
                    exclude_keywords=filter_config.exclude_keywords,
                ),
            )
            engine = EventIngestionEngine(
                source_adapter=adapter,
                storage_adapter=self._storage,
                publisher=self._publisher,
                producer="news_fetcher",
                event_type="news.article.created",
                filter_run=filter_config.production_filter_run(),
                dedupe_policy=SoftDedupePolicy(
                    enabled=True,
                    algorithm=filter_config.dedupe_algorithm,
                    threshold=filter_config.dedupe_similarity_threshold,
                    lookback_window=timedelta(hours=filter_config.dedupe_lookback_hours),
                    max_time_delta_hours=filter_config.dedupe_lookback_hours,
                ),
            )
            try:
                result = engine.process_source(source_key)
                results[source_key] = result
                self._mark_interval(source_key)
                next_retry_at = self._source_interval_until.get(source_key)
                LOGGER.info(
                    "news_fetcher source fetch attempt completed source_key=%s status=success "
                    "fetched=%s accepted=%s rejected=%s checkpoint_advanced=%s next_retry_at=%s",
                    source_key,
                    result.fetched,
                    result.accepted,
                    result.rejected,
                    result.checkpoint_advanced,
                    _isoformat(next_retry_at),
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=result,
                    status="success",
                    error_code=None,
                    fetch_attempt_at=started_at,
                )
            except ProviderRateLimitError:
                backoff_until = datetime.now(timezone.utc) + timedelta(
                    seconds=self._settings.rss_rate_limit_backoff_seconds
                )
                self._source_backoff_until[source_key] = backoff_until
                LOGGER.warning(
                    "news_fetcher source fetch attempt failed source_key=%s error_code=provider_rate_limited "
                    "next_retry_at=%s",
                    source_key,
                    _isoformat(backoff_until),
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="provider_rate_limited",
                    fetch_attempt_at=started_at,
                )
                continue
            except Exception:  # pragma: no cover - defensive process-level guard
                self._mark_interval(source_key)
                next_retry_at = self._source_interval_until.get(source_key)
                LOGGER.exception(
                    "news_fetcher source fetch attempt failed source_key=%s error_code=source_cycle_failed "
                    "next_retry_at=%s",
                    source_key,
                    _isoformat(next_retry_at),
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="source_cycle_failed",
                    fetch_attempt_at=started_at,
                )
                continue

        return results

    def _drain_retryable_obligations(self) -> None:
        claimer = getattr(self._storage, "claim_retryable_obligations", None)
        if claimer is None:
            return
        try:
            obligations = claimer(
                worker_id=self._retry_worker_id,
                limit=max(1, self._settings.publish_retry_drain_batch_size),
                lease_seconds=_RETRY_DRAIN_LEASE_SECONDS,
            )
        except Exception:
            LOGGER.exception("failed to claim retryable publication obligations")
            return

        for obligation in obligations:
            try:
                self._publisher.publish(obligation.envelope_json)
                self._storage.mark_obligation_status(
                    obligation_id=obligation.obligation_id,
                    status=PublicationStatus.PUBLISHED,
                    last_error_code=None,
                )
            except NonTransientPublishError as error:
                self._storage.mark_obligation_status(
                    obligation_id=obligation.obligation_id,
                    status=PublicationStatus.DEAD_LETTERED,
                    last_error_code=str(error) or "non_transient_publish_error",
                )
            except TransientPublishError as error:
                self._storage.mark_obligation_status(
                    obligation_id=obligation.obligation_id,
                    status=PublicationStatus.PENDING,
                    last_error_code=str(error) or "transient_publish_error",
                )
            except Exception:
                LOGGER.exception(
                    "failed to drain retryable publication obligation",
                    extra={"obligation_id": obligation.obligation_id, "source_key": obligation.source_key},
                )
                self._storage.mark_obligation_status(
                    obligation_id=obligation.obligation_id,
                    status=PublicationStatus.PENDING,
                    last_error_code="unexpected_publish_error",
                )

    def _production_filter_config(self, watchlist: set[str]):
        loader = getattr(self._storage, "seed_production_filter_config_if_missing", None)
        if loader is None:
            return _settings_filter_config(self._settings, watchlist)
        return loader(
            include_keywords=self._settings.include_keywords,
            exclude_keywords=self._settings.exclude_keywords,
            watchlist_tickers=watchlist,
            dedupe_algorithm=self._settings.dedupe_algorithm,
            dedupe_similarity_threshold=self._settings.dedupe_similarity_threshold,
            dedupe_lookback_hours=self._settings.dedupe_lookback_hours,
        )

    def _configured_providers(self) -> dict[str, NewsProvider]:
        providers = dict(self._providers)
        if self._settings.rss_enabled:
            loader = getattr(self._storage, "load_rss_feed_specs", None)
            if loader is not None:
                for spec in loader():
                    providers[spec.source_key] = RssProvider(feed_spec=spec)
                    self._source_min_interval_seconds[spec.source_key] = spec.min_request_interval_seconds
        self._register_company_news_providers(providers)
        return providers

    def _register_company_news_providers(self, providers: dict[str, NewsProvider]) -> None:
        if not self._settings.finnhub_company_news_enabled:
            return
        api_key = self._settings.finnhub_api_key
        if not api_key:
            return
        loader = getattr(self._storage, "load_active_watchlist_rows", None)
        if loader is None:
            return

        allowed_exchanges = set(self._settings.finnhub_company_news_exchange_codes)
        for row in loader():
            if allowed_exchanges and row.exchange_code not in allowed_exchanges:
                continue
            source_key = finnhub_company_news_source_key(
                ticker=row.ticker,
                exchange_code=row.exchange_code,
            )
            providers[source_key] = FinnhubCompanyNewsProvider(api_key=api_key, ticker=row.ticker)
            self._source_min_interval_seconds[source_key] = (
                self._settings.finnhub_company_news_min_interval_seconds
            )

    def _is_backing_off(self, source_key: str, now: datetime) -> bool:
        backoff_until = self._source_backoff_until.get(source_key)
        if backoff_until is None:
            return False
        if now < backoff_until:
            return True
        self._source_backoff_until.pop(source_key, None)
        return False

    def _is_waiting_for_interval(self, source_key: str, now: datetime) -> bool:
        interval_until = self._source_interval_until.get(source_key)
        if interval_until is None:
            return False
        if now < interval_until:
            return True
        self._source_interval_until.pop(source_key, None)
        return False

    def _mark_interval(self, source_key: str) -> None:
        interval_seconds = self._source_min_interval_seconds.get(source_key, 0)
        if interval_seconds <= 0:
            return
        self._source_interval_until[source_key] = datetime.now(timezone.utc) + timedelta(
            seconds=interval_seconds
        )

    def _record_cycle_status(
        self,
        *,
        source_key: str,
        started_at: datetime,
        result: ProcessResult | None,
        status: str,
        error_code: str | None,
        fetch_attempt_at: datetime | None = None,
    ) -> None:
        recorder = getattr(self._storage, "record_provider_cycle_status", None)
        if recorder is None:
            return

        fetched = result.fetched if result else 0
        accepted = result.accepted if result else 0
        rejected = result.rejected if result else 0
        checkpoint_advanced = result.checkpoint_advanced if result else False
        finished_at = datetime.now(timezone.utc)
        last_non_zero_fetch_at = finished_at if fetched > 0 else None

        try:
            recorder(
                source_key=source_key,
                started_at=started_at,
                finished_at=finished_at,
                last_non_zero_fetch_at=last_non_zero_fetch_at,
                status=status,
                error_code=error_code,
                last_fetch_attempt_at=fetch_attempt_at,
                fetched_count=fetched,
                accepted_count=accepted,
                rejected_count=rejected,
                checkpoint_advanced=checkpoint_advanced,
            )
        except Exception:
            LOGGER.exception("failed to record news_fetcher cycle status", extra={"source_key": source_key})


def build_service(
    *,
    settings: NewsFetcherSettings,
    providers: dict[str, NewsProvider],
) -> NewsFetcherService:
    """Construct default NewsFetcher service with PostgreSQL and Redis adapters."""
    instrument_registry = PostgresSharedInstrumentRegistry(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
        watchlist_table=settings.watchlist_table,
    )
    storage = PostgresNewsStorageAdapter(
        dsn=settings.postgres_dsn,
        news_schema=settings.newsfetcher_db_schema,
        instrument_registry=instrument_registry,
    )
    publisher = RedisStreamPublisher(
        queue_url=settings.queue_url,
        stream_name=settings.news_raw_queue,
    )
    return NewsFetcherService(
        settings=settings,
        providers=providers,
        storage=storage,
        publisher=publisher,
    )


def _settings_filter_config(settings: NewsFetcherSettings, watchlist: set[str]):
    from src.product_components.news_fetcher.filter_config import NewsFilterConfig, normalize_keywords, normalize_tickers

    return NewsFilterConfig(
        filter_config_id="env_fallback",
        config_name="Environment fallback",
        config_role="production",
        status="active",
        include_keywords=normalize_keywords(settings.include_keywords),
        exclude_keywords=normalize_keywords(settings.exclude_keywords),
        watchlist_tickers=normalize_tickers(watchlist),
        dedupe_algorithm=settings.dedupe_algorithm,
        dedupe_similarity_threshold=settings.dedupe_similarity_threshold,
        dedupe_lookback_hours=settings.dedupe_lookback_hours,
    )


def _isoformat(value: datetime | None) -> str:
    if value is None:
        return "next_poll_cycle"
    return value.isoformat()
