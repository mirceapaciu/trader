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
```

## Shared Dependencies

NewsFetcher also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.
