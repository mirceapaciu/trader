-- NewsFetcher physical schema (PostgreSQL)

CREATE SCHEMA IF NOT EXISTS news_fetcher;

CREATE TABLE IF NOT EXISTS news_fetcher.t_news_articles (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    url TEXT NOT NULL,
    tickers JSONB,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    sentiment_source DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS news_fetcher.t_source_checkpoints (
    source_key TEXT PRIMARY KEY,
    cursor_value JSONB NOT NULL,
    cursor_updated_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
