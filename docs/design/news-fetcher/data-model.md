# NewsFetcher Data Model

Tables owned by the NewsFetcher component.
PostgreSQL schema: `news_fetcher`.

## Tables

```sql
CREATE SCHEMA IF NOT EXISTS news_fetcher;

CREATE TABLE IF NOT EXISTS news_fetcher.news_articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    url TEXT NOT NULL,
    tickers JSONB,            -- JSON array
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    sentiment_source DOUBLE PRECISION
);
```
