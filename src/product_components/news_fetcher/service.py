from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.core_components.event_ingestion_engine.engine import EventIngestionEngine
from src.core_components.event_ingestion_engine.interfaces import EventPublisher, StorageAdapter
from src.core_components.event_ingestion_engine.models import ProcessResult, SoftDedupePolicy

from .providers import NewsProvider
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

    def run_once(self) -> dict[str, ProcessResult]:
        results: dict[str, ProcessResult] = {}
        watchlist = self._storage.load_active_watchlist_tickers()

        for source_key, provider in self._providers.items():
            started_at = datetime.now(timezone.utc)
            adapter = NewsSourceAdapter(
                provider=provider,
                timeout_seconds=self._settings.provider_timeout_seconds,
                filter_config=SourceFilterConfig(
                    watchlist_tickers=watchlist,
                    include_keywords=self._settings.include_keywords,
                    exclude_keywords=self._settings.exclude_keywords,
                ),
            )
            engine = EventIngestionEngine(
                source_adapter=adapter,
                storage_adapter=self._storage,
                publisher=self._publisher,
                producer="news_fetcher",
                event_type="news.article.created",
                dedupe_policy=SoftDedupePolicy(
                    enabled=True,
                    algorithm=self._settings.dedupe_algorithm,
                    threshold=self._settings.dedupe_similarity_threshold,
                    lookback_window=timedelta(hours=self._settings.dedupe_lookback_hours),
                    max_time_delta_hours=self._settings.dedupe_lookback_hours,
                ),
            )
            try:
                result = engine.process_source(source_key)
                results[source_key] = result
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=result,
                    status="success",
                    error_code=None,
                )
            except Exception:  # pragma: no cover - defensive process-level guard
                LOGGER.exception("news_fetcher source cycle failed", extra={"source_key": source_key})
                self._record_cycle_status(
                    source_key=source_key,
                    started_at=started_at,
                    result=None,
                    status="error",
                    error_code="source_cycle_failed",
                )
                continue

        return results

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
