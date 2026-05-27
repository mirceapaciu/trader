# NewsFetcher Configuration

Configuration owned by the NewsFetcher process.

## Environment Variables

```bash
# Database ownership
NEWSFETCHER_DB_SCHEMA=news_fetcher

# News feeds
FINNHUB_API_KEY=
MARKETAUX_API_KEY=           # Optional

# Scheduling
NEWS_POLL_INTERVAL=120       # seconds
RSS_POLL_INTERVAL=300        # seconds
MARKETAUX_POLL_INTERVAL=300  # seconds (used only when Marketaux is enabled)
PREPOST_POLL_INTERVAL=600    # seconds
MARKET_HOURS_ONLY=false      # when false, run continuous polling and ignore market session gating


# Provider controls
PROVIDER_TIMEOUT_SECONDS=10
PROVIDER_MAX_RETRIES=3
PROVIDER_BACKOFF_BASE_SECONDS=1

# Provider-specific inputs
RSS_FEED_URLS=               # comma-separated URLs

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

Watchlist source for relevance filtering is the shared schema table `shared.t_watchlist_tickers` (table name configurable via `WATCHLIST_TABLE`).
