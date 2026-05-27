from __future__ import annotations

import logging
from datetime import timedelta

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
            except Exception:  # pragma: no cover - defensive process-level guard
                LOGGER.exception("news_fetcher source cycle failed", extra={"source_key": source_key})
                continue

        return results


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
