# NewsFetcher Configuration

Configuration owned by the NewsFetcher process.

Provider-specific source guidance, including Yahoo Finance RSS URL rules, is documented in `docs/design/product_components/news-fetcher/news-sources.md`.

## Environment Variables

```bash
# Database ownership
NEWSFETCHER_DB_SCHEMA=news_fetcher

# News feeds
FINNHUB_API_KEY=
MARKETAUX_API_KEY=           # Optional
NEWS_INSTRUMENTS_CONFIG=config/news-fetcher/instruments.json
NEWS_RSS_SOURCES_CONFIG=config/news-fetcher/rss-sources.json

# Scheduling
NEWS_POLL_INTERVAL=120       # seconds
RSS_POLL_INTERVAL=300        # seconds
MARKETAUX_POLL_INTERVAL=300  # seconds (used only when Marketaux is enabled)
RSS_RATE_LIMIT_BACKOFF_SECONDS=900  # seconds to suppress one RSS source after HTTP 429

# Finnhub per-symbol company news (one source per active watchlist listing)
NEWS_SOURCE_FINNHUB_COMPANY_NEWS_ENABLED=true
FINNHUB_COMPANY_NEWS_MIN_INTERVAL_SECONDS=120  # per-symbol minimum seconds between fetches
FINNHUB_COMPANY_NEWS_EXCHANGES=XNAS,XNYS       # exchange codes polled (bare tickers are valid Finnhub symbols only for US listings)
PREPOST_POLL_INTERVAL=600    # seconds
MARKET_HOURS_ONLY=false      # when false, run continuous polling and ignore market session gating

# Logging
NEWS_FETCHER_LOG_LEVEL=INFO
NEWS_FETCHER_LOG_FILE=logs/news-fetcher.log


# Provider controls
PROVIDER_TIMEOUT_SECONDS=10
PROVIDER_MAX_RETRIES=3
PROVIDER_BACKOFF_BASE_SECONDS=1
NEWS_PUBLISH_RETRY_DRAIN_BATCH_SIZE=500

# Provider-specific inputs
RSS_FEED_URLS=               # deprecated comma-separated static/broad RSS fallback

# Relevance filtering
NEWS_INCLUDE_KEYWORDS=       # comma-separated case-insensitive terms
NEWS_EXCLUDE_KEYWORDS=       # comma-separated case-insensitive terms

# Deduplication
DEDUPE_LOOKBACK_HOURS=24
DEDUPE_SIMILARITY_THRESHOLD=0.9
DEDUPE_ALGORITHM=rapidfuzz_ratio

# Checkpoint bootstrap
CHECKPOINT_BOOTSTRAP_MODE=latest   # latest | lookback
CHECKPOINT_BOOTSTRAP_LOOKBACK_HOURS=24
```

## Shared Dependencies

NewsFetcher also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.

Watchlist source for relevance filtering is the Shared Instrument Registry API or the documented shared watchlist adapter. Physical shared table names are implementation details of that contract and must not be copied into ad hoc component queries.

NewsFetcher seeds source configuration from JSON into PostgreSQL during startup:

- `NEWS_INSTRUMENTS_CONFIG` defines reusable instrument names, aliases, and identifiers.
- `NEWS_RSS_SOURCES_CONFIG` defines static RSS feeds, dynamic Yahoo RSS sources, and provider-specific symbol mappings.

Runtime reads RSS configuration from NewsFetcher-owned RSS tables and retrieves reusable aliases through the Shared Instrument Registry contract. `RSS_FEED_URLS` remains available only as a legacy fallback and is synced into DB as static RSS sources.
