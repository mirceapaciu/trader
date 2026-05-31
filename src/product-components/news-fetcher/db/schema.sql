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

CREATE TABLE IF NOT EXISTS news_fetcher.t_provider_cycle_status (
    source_key TEXT PRIMARY KEY,
    last_cycle_started_at TIMESTAMPTZ NOT NULL,
    last_cycle_finished_at TIMESTAMPTZ NOT NULL,
    last_cycle_status TEXT NOT NULL,
    last_cycle_error_code TEXT,
    last_cycle_fetched_count INTEGER NOT NULL DEFAULT 0,
    last_cycle_accepted_count INTEGER NOT NULL DEFAULT 0,
    last_cycle_rejected_count INTEGER NOT NULL DEFAULT 0,
    last_cycle_checkpoint_advanced BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_provider_cycle_status
        CHECK (last_cycle_status IN ('success', 'error')),
    CONSTRAINT ck_provider_cycle_counts
        CHECK (
            last_cycle_fetched_count >= 0
            AND last_cycle_accepted_count >= 0
            AND last_cycle_rejected_count >= 0
        )
);

CREATE TABLE IF NOT EXISTS news_fetcher.t_rss_sources (
    source_key TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'dynamic_watchlist',
    symbol_param TEXT NOT NULL DEFAULT 's',
    default_query_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_symbols_per_request INTEGER NOT NULL DEFAULT 10,
    min_request_interval_seconds INTEGER NOT NULL DEFAULT 900,
    grouping_mode TEXT NOT NULL DEFAULT 'grouped',
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_rss_sources_max_symbols
        CHECK (max_symbols_per_request > 0),
    CONSTRAINT ck_rss_sources_min_interval
        CHECK (min_request_interval_seconds >= 0),
    CONSTRAINT ck_rss_sources_source_type
        CHECK (source_type IN ('static', 'dynamic_watchlist')),
    CONSTRAINT ck_rss_sources_grouping_mode
        CHECK (grouping_mode IN ('grouped', 'single', 'static'))
);

ALTER TABLE news_fetcher.t_rss_sources
    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'dynamic_watchlist';

ALTER TABLE news_fetcher.t_rss_sources
    ADD COLUMN IF NOT EXISTS max_symbols_per_request INTEGER NOT NULL DEFAULT 10;

ALTER TABLE news_fetcher.t_rss_sources
    ADD COLUMN IF NOT EXISTS min_request_interval_seconds INTEGER NOT NULL DEFAULT 900;

ALTER TABLE news_fetcher.t_rss_sources
    ADD COLUMN IF NOT EXISTS grouping_mode TEXT NOT NULL DEFAULT 'grouped';

ALTER TABLE news_fetcher.t_rss_sources
    DROP CONSTRAINT IF EXISTS ck_rss_sources_grouping_mode;

ALTER TABLE news_fetcher.t_rss_sources
    ADD CONSTRAINT ck_rss_sources_grouping_mode
        CHECK (grouping_mode IN ('grouped', 'single', 'static'));

CREATE TABLE IF NOT EXISTS news_fetcher.t_rss_symbol_rules (
    source_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    query_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    match_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_key, ticker, exchange_code),
    CONSTRAINT fk_rss_symbol_rules_source
        FOREIGN KEY (source_key)
        REFERENCES news_fetcher.t_rss_sources (source_key)
        ON DELETE CASCADE
);

ALTER TABLE news_fetcher.t_rss_symbol_rules
    ADD COLUMN IF NOT EXISTS match_terms JSONB NOT NULL DEFAULT '[]'::jsonb;

INSERT INTO news_fetcher.t_rss_sources (
    source_key,
    base_url,
    source_type,
    symbol_param,
    default_query_params,
    max_symbols_per_request,
    min_request_interval_seconds,
    grouping_mode,
    is_enabled
)
VALUES (
    'yahoo_finance',
    'https://feeds.finance.yahoo.com/rss/2.0/headline',
    'dynamic_watchlist',
    's',
    '{"region": "US", "lang": "en-US"}'::jsonb,
    10,
    900,
    'grouped',
    TRUE
)
ON CONFLICT (source_key) DO NOTHING;

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
