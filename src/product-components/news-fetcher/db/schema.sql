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

CREATE TABLE IF NOT EXISTS news_fetcher.t_publication_obligations (
    obligation_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    envelope_json JSONB NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    claimed_by TEXT,
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_publication_obligation_semantic
        UNIQUE (canonical_event_id, event_type, dedupe_key),
    CONSTRAINT fk_publication_obligation_canonical_event
        FOREIGN KEY (canonical_event_id)
        REFERENCES news_fetcher.t_news_articles (id)
        ON DELETE CASCADE,
    CONSTRAINT ck_publication_obligation_status
        CHECK (status IN ('pending', 'publishing', 'published', 'dead_lettered')),
    CONSTRAINT ck_publication_obligation_attempt_count
        CHECK (attempt_count >= 0),
    CONSTRAINT ck_publication_obligation_claim_pair
        CHECK (
            (claimed_by IS NULL AND claim_expires_at IS NULL)
            OR (claimed_by IS NOT NULL AND claim_expires_at IS NOT NULL)
        ),
    CONSTRAINT ck_publication_obligation_terminal_claim_clear
        CHECK (
            status NOT IN ('published', 'dead_lettered')
            OR (claimed_by IS NULL AND claim_expires_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_pub_obligations_unresolved_by_source
    ON news_fetcher.t_publication_obligations (source_key, status, updated_at, obligation_id)
    WHERE status IN ('pending', 'publishing');

CREATE INDEX IF NOT EXISTS idx_pub_obligations_claim_scan
    ON news_fetcher.t_publication_obligations (status, updated_at, obligation_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_pub_obligations_stale_lease_scan
    ON news_fetcher.t_publication_obligations (status, claim_expires_at, updated_at, obligation_id)
    WHERE status = 'publishing';

CREATE INDEX IF NOT EXISTS idx_pub_obligations_batch_gate
    ON news_fetcher.t_publication_obligations (batch_id, status);
