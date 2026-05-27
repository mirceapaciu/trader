"""NewsFetcher runtime component."""

from .providers import (
    FinnhubProvider,
    MarketauxProvider,
    NewsProvider,
    ProviderArticle,
    ProviderBatch,
    RssProvider,
)
from .service import NewsFetcherService, build_service
from .settings import NewsFetcherSettings
from .source_adapter import NewsSourceAdapter, SourceFilterConfig
from .storage_adapter import PostgresNewsStorageAdapter

__all__ = [
    "NewsFetcherService",
    "NewsFetcherSettings",
    "FinnhubProvider",
    "MarketauxProvider",
    "NewsProvider",
    "ProviderArticle",
    "ProviderBatch",
    "RssProvider",
    "NewsSourceAdapter",
    "SourceFilterConfig",
    "PostgresNewsStorageAdapter",
    "build_service",
]
