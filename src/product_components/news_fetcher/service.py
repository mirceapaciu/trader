from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.core_components.event_ingestion_engine.engine import EventIngestionEngine
from src.core_components.event_ingestion_engine.interfaces import EventPublisher, StorageAdapter
from src.core_components.event_ingestion_engine.models import ProcessResult, SoftDedupePolicy

from .providers import NewsProvider, ProviderRateLimitError, RssProvider
from .publisher import RedisStreamPublisher
from .settings import NewsFetcherSettings
from .source_adapter import NewsSourceAdapter, SourceFilterConfig
from .storage_adapter import PostgresNewsStorageAdapter

LOGGER = logging.getLogger(__name__)


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

    def run_once(self) -> dict[str, ProcessResult]:
        results: dict[str, ProcessResult] = {}
        watchlist = self._storage.load_active_watchlist_tickers()
        filter_config = self._production_filter_config(watchlist)
        providers = self._configured_providers()

        for source_key, provider in providers.items():
            started_at = datetime.now(timezone.utc)
            if self._is_backing_off(source_key, started_at):
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="provider_rate_limit_backoff",
                )
                continue
            if self._is_waiting_for_interval(source_key, started_at):
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="success",
                    error_code="source_interval_wait",
                )
                continue

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
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=result,
                    status="success",
                    error_code=None,
                )
            except ProviderRateLimitError:
                backoff_until = datetime.now(timezone.utc) + timedelta(
                    seconds=self._settings.rss_rate_limit_backoff_seconds
                )
                self._source_backoff_until[source_key] = backoff_until
                LOGGER.warning(
                    "news_fetcher source rate limited; backing off",
                    extra={"source_key": source_key, "backoff_until": backoff_until.isoformat()},
                )
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="provider_rate_limited",
                )
                continue
            except Exception:  # pragma: no cover - defensive process-level guard
                LOGGER.exception("news_fetcher source cycle failed", extra={"source_key": source_key})
                self._mark_interval(source_key)
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="source_cycle_failed",
                )
                continue

        return results

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
        if not self._settings.rss_enabled:
            return providers

        loader = getattr(self._storage, "load_rss_feed_specs", None)
        if loader is None:
            return providers

        for spec in loader():
            providers[spec.source_key] = RssProvider(feed_spec=spec)
            self._source_min_interval_seconds[spec.source_key] = spec.min_request_interval_seconds
        return providers

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
    ) -> None:
        recorder = getattr(self._storage, "record_provider_cycle_status", None)
        if recorder is None:
            return

        fetched = result.fetched if result else 0
        accepted = result.accepted if result else 0
        rejected = result.rejected if result else 0
        checkpoint_advanced = result.checkpoint_advanced if result else False

        try:
            recorder(
                source_key=source_key,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                status=status,
                error_code=error_code,
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
    storage = PostgresNewsStorageAdapter(
        dsn=settings.postgres_dsn,
        news_schema=settings.newsfetcher_db_schema,
        shared_schema=settings.shared_db_schema,
        watchlist_table=settings.watchlist_table,
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
