# NewsFetcher Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the NewsFetcher process.

Provider selection and source-specific rules are documented in `docs/design/product-components/news-fetcher/news-sources.md`.

NewsFetcher responsibilities:
- Fetch news from configured providers.
- Normalize all provider payloads into one canonical article shape.
- Persist all structurally valid normalized candidates into a 30-day input corpus.
- Apply production filtering and deduplication without blocking future simulation runs.
- Persist production filter outcomes and accepted canonical articles in schema news_fetcher.
- Publish canonical events to queue news_raw_queue.
- Record provider usage in schema shared. (For measuring the external API consumption)
- Preserve stable article identifiers for thesis-card evidence linkage.

Out of scope:
- LLM analysis and sentiment inference beyond provider-supplied fields.
- Trade decision logic.
- Trade execution.

## 2. Process Contract

Process name: news_fetcher

Inputs:
- External provider APIs and RSS feeds.
- Environment configuration.
- Watchlist loaded from shared schema table `shared.t_watchlist_tickers`.

Outputs:
- Rows in news_fetcher.t_input_news_articles.
- Rows in news_fetcher.t_news_articles.
- Rows in news_fetcher.t_news_filter_runs and news_fetcher.t_news_filter_results for the production baseline.
- Optional rows in shared.t_api_usage for provider usage tracking.
- Events published to news_raw_queue.

Delivery semantics:
- At-least-once publish to the broker.
- Idempotent writes and publishes by deterministic article id.

## 3. Scheduling and Runtime Windows

Default scheduler rules:
- Finnhub poll interval is controlled by `NEWS_POLL_INTERVAL`.
- RSS poll interval is controlled by `RSS_POLL_INTERVAL`.
- Marketaux poll interval is controlled by `MARKETAUX_POLL_INTERVAL` when enabled.
- Recommended defaults are documented in the NewsFetcher configuration spec.

Market-hour behavior:
- If `MARKET_HOURS_ONLY=true`:
  - During market hours, run default intervals.
  - During pre and post market, use slower intervals if configured.
  - On weekends and exchange holidays, do not poll.
- If `MARKET_HOURS_ONLY=false`:
  - Ignore market session gating and run configured provider intervals continuously.
  - `PREPOST_POLL_INTERVAL` is ignored.

Market calendar and timezone contract:
- This contract applies only when `MARKET_HOURS_ONLY=true`.
- Market-hours evaluation must use the exchange timezone from `MARKET_TIMEZONE`.
- Holidays and trading sessions must be evaluated using `MARKET_CALENDAR`.
- If calendar resolution fails, process must log warning and use default intervals for safety.

Startup behavior:
- Validate required configuration.
- Open database and queue connections.
- Start independent polling loops per provider.

Shutdown behavior:
- Stop accepting new polling cycles.
- Finish in-flight cycle with timeout.
- Flush pending writes and publishes.
- Close all connections.

## 4. Provider Fetch Rules

### 4.1 Common Fetch Rules

For each provider call:
- Set request timeout from configuration.
- Retry transient failures with exponential backoff.
- Respect provider quota and local rate limits.
- Emit one usage record on each completed call attempt where possible.

Transient failures include:
- HTTP 408, 429, 500, 502, 503, 504.
- Socket timeout.
- Connection reset.

HTTP 429 handling:
- Treat provider rate limits as source-local failures.
- Record the provider cycle with error code `provider_rate_limited`.
- Suppress that source until `RSS_RATE_LIMIT_BACKOFF_SECONDS` has elapsed.
- During suppression, record cycle status with error code `provider_rate_limit_backoff`.

Non-transient failures include:
- 400, 401, 403, 404.
- Schema validation errors for malformed payloads.

### 4.2 Finnhub

- Primary source for company and market headlines.
- Use last successful checkpoint for incremental fetch.
- On quota exhaustion, skip until next interval and continue with other sources.

### 4.5 Checkpoint Lifecycle

Checkpoint table:
- `news_fetcher.t_source_checkpoints`

Source key examples:
- `finnhub`
- `marketaux`
- `rss:static`
- `rss:yahoo_finance:<ticker>:<exchange_code>` when an RSS source uses single-symbol grouping mode.
- `rss:yahoo_finance:batch:<hash>` when an RSS source uses grouped-symbol mode.

Bootstrap policy:
- If checkpoint exists, start from stored `cursor_value`.
- If checkpoint does not exist, initialize from configured bootstrap mode.

Bootstrap mode contract:
- `CHECKPOINT_BOOTSTRAP_MODE=latest`: start from provider latest cursor and ingest only new arrivals after startup.
- `CHECKPOINT_BOOTSTRAP_MODE=lookback`: initialize cursor from `CHECKPOINT_BOOTSTRAP_LOOKBACK_HOURS` window and ingest recent history.
- Default mode is `latest`.

Advance policy:
- Advance checkpoint only after the processed batch is durably persisted and publish obligations are completed.
- If persistence or publish fails for the batch, do not advance checkpoint.
- Use optimistic concurrency on checkpoint `version` to prevent lost updates.

Replay policy:
- Reprocessing from older checkpoints is supported for recovery and backfill.
- Replays rely on idempotent article upsert and idempotent event handling.
- Manual checkpoint rewind operations must be logged with operator identity and reason.

### 4.3 RSS

- Parse configured static feed URLs and dynamically generated RSS feed URLs.
- Generate ticker-specific Yahoo Finance RSS URLs from active watchlist rows and DB-backed RSS rules.
- Use grouped Yahoo RSS requests by default, with comma-separated provider symbols in one `s=` query parameter.
- Normalize publication time to UTC.
- Tag grouped-feed articles only when article text matches configured ticker terms.
- If no ticker metadata is present, use keyword extraction and watchlist matching.

### 4.4 Marketaux

- Optional source enabled only when API key is provided.
- Use as supplement when Finnhub does not cover relevant tickers.

## 5. Canonical Article Contract

Each fetched item is normalized to this canonical article object before further processing:

- id: deterministic hash of source and canonical URL.
- source: finnhub, marketaux, or rss.
- headline: required non-empty string.
- summary: optional string.
- url: required canonical URL.
- tickers: JSON array of uppercase symbols, deduplicated.
- published_at: required UTC timestamp.
- fetched_at: required UTC timestamp at ingest.
- sentiment_source: optional provider score when available.

Constraint-facing requirement:
- The article contract must preserve enough context quality for AnalyzerWorker to produce exactly three thesis-card evidence bullets with article references.

Normalization rules:
- Trim whitespace for headline and summary.
- Canonicalize URL by removing tracking query parameters.
- Convert ticker symbols to uppercase.
- Drop empty ticker strings.
- Drop records with empty headline or invalid URL.

## 6. Filtering Rules

Filter order:
1. Structural validation.
2. Persist structurally valid normalized candidates into `t_input_news_articles`.
3. Watchlist and keyword relevance.
4. Duplicate detection.

Relevance rules:
- Keep if at least one ticker intersects watchlist.
- Keep if headline or summary matches configured keywords.
- Drop otherwise.

Configuration must support:
- Shared watchlist table (`shared.t_watchlist_tickers`, table name via `WATCHLIST_TABLE`).
- Include keywords. 
- Exclude keywords.

## 7. Deduplication Rules

Deduplication key types:
- Strong duplicate: identical canonical URL.
- Soft duplicate: fuzzy similarity of normalized headline within a time window.

Strong duplicate policy:
- If URL already exists in the recent lookback window, drop as duplicate.

Soft duplicate policy:
- Compare headline against recent articles from the same source and tickers.
- Use configurable similarity algorithm and threshold.
- If similarity exceeds threshold inside the lookback window, drop as duplicate.

Minimum required configuration:
- DEDUPE_LOOKBACK_HOURS
- DEDUPE_SIMILARITY_THRESHOLD
- DEDUPE_ALGORITHM

## 8. Persistence Rules

Primary table:
- news_fetcher.t_input_news_articles
- news_fetcher.t_news_articles
- news_fetcher.t_news_filter_results

Write policy:
- Persist all structurally valid normalized candidates to `t_input_news_articles`.
- Persist production filter outcome for each candidate to `t_news_filter_results` under the production run.
- Persist only accepted candidates to `t_news_articles`.
- On conflict do not mutate source publication metadata.
- Always preserve first seen fetched_at.

Transaction policy:
- For each batch, persist input-corpus rows, production filter-result rows, accepted article rows, and publication obligations in one database transaction.
- Publisher worker claims pending obligations and publishes envelope to queue.
- On successful publish, obligation transitions to `published`.
- If publish fails, retry by incrementing `attempt_count` and preserving idempotent `dedupe_key`.
- If retries are exhausted, obligation transitions to `dead_lettered` and envelope is routed to `failed_messages_dlq` when available.
- Checkpoint can advance only when all obligations for the processed batch are terminal (`published` or `dead_lettered`).

Recommended indexes:
- published_at descending.
- source and published_at.
- GIN index on tickers.

Checkpoint persistence rules:
- Store one durable checkpoint row per `source_key` in `t_source_checkpoints`.
- `cursor_value` must contain the provider-specific incremental cursor as JSON.
- `cursor_updated_at` must reflect the latest event boundary included by the checkpoint.
- Update checkpoint row atomically using `version` compare-and-set.

## 9. Queue Publish Contract

Target queue:
- news_raw_queue

Published event schema:
- event_type: news.article.created
- event_version: 1
- event_id: deterministic from article id and event_type
- occurred_at: UTC timestamp
- dedupe_key: article id
- producer: news_fetcher
- payload:
  - article_id
  - source
  - headline
  - summary
  - url
  - tickers
  - published_at
  - fetched_at
  - sentiment_source

Publish rules:
- Publish only for accepted articles from durable obligations created after successful persistence.
- Use dedupe_key so consumers can enforce idempotency.
- Retry publish using queue retry policy.
- On retry exhaustion, send event envelope to failed_messages_dlq.

## 10. Error Handling and Recovery

Database failures:
- Retry once for transient connection errors.
- If persistent, mark process unhealthy and stop publishing.

Queue failures:
- Retry with exponential backoff up to QUEUE_MAX_RETRIES.
- On exhaustion, write failed envelope to failed_messages_dlq when available.

Provider failures:
- Log warning with provider and status code.
- Continue other providers in same cycle.
- Do not terminate process for single-provider failure.

Poison payloads:
- Log structured validation failure with source and raw identifier.
- Skip payload.
- Continue cycle.

## 11. Observability

Required logs:
- Poll cycle start and end with provider and duration.
- Number fetched, filtered, deduplicated, persisted, and published.
- Retry attempts and final outcomes.
- Health state transitions.

Required metrics:
- fetch_count by provider.
- fetch_error_count by provider and class.
- dedupe_drop_count by type strong or soft.
- persist_success_count and persist_error_count.
- publish_success_count and publish_error_count.
- end_to_end_latency_seconds from published_at to fetched_at.

Usage accounting:
- When provider usage data is available, write to shared.t_api_usage.
- For RSS sources where token or cost is not applicable, set nullable fields.

## 12. Configuration Requirements

Required variables already defined elsewhere:
- FINNHUB_API_KEY
- NEWS_POLL_INTERVAL
- RSS_POLL_INTERVAL
- NEWSFETCHER_DB_SCHEMA
- SHARED_DB_SCHEMA
- WATCHLIST_TABLE
- QUEUE_BACKEND
- QUEUE_URL
- NEWS_RAW_QUEUE
- FAILED_MESSAGES_DLQ
- QUEUE_MAX_RETRIES
- Database connection variables

Required additions for complete behavior control:
- RSS_FEED_URLS
- NEWS_INCLUDE_KEYWORDS
- NEWS_EXCLUDE_KEYWORDS
- MARKETAUX_POLL_INTERVAL
- PROVIDER_TIMEOUT_SECONDS
- PROVIDER_MAX_RETRIES
- PROVIDER_BACKOFF_BASE_SECONDS
- DEDUPE_LOOKBACK_HOURS
- DEDUPE_SIMILARITY_THRESHOLD
- DEDUPE_ALGORITHM
- MARKET_HOURS_ONLY
- PREPOST_POLL_INTERVAL
- MARKET_TIMEZONE (required only when `MARKET_HOURS_ONLY=true`)
- MARKET_CALENDAR (required only when `MARKET_HOURS_ONLY=true`)
- CHECKPOINT_BOOTSTRAP_MODE
- CHECKPOINT_BOOTSTRAP_LOOKBACK_HOURS

## 13. Health and Readiness

Readiness is healthy when:
- Database connection is valid.
- Queue connection is valid.
- At least one provider is enabled and validated.

Liveness is healthy when:
- Scheduler loop is active.
- Last successful cycle is within two poll intervals for at least one enabled provider.

## 14. Acceptance Criteria

Implementation is acceptable when all are true:
- Deterministic canonical article id is stable for same source and URL.
- Duplicate suppression works for both strong and soft duplicates.
- Persist then publish ordering is enforced.
- Publish retries and dead-letter routing work as configured.
- Process continues when one provider fails.
- Structured logs and metrics are emitted for each cycle.
- Required unit tests and integration tests pass.

## 15. Minimum Test Plan

Unit tests:
- Normalization and canonical id stability.
- URL canonicalization and ticker normalization.
- Relevance filtering with watchlist and keyword combinations.
- Strong and soft dedupe behavior around threshold boundaries.
- Retry and backoff behavior classification.

Integration tests:
- End-to-end cycle with local PostgreSQL and Redis.
- Upsert idempotency across repeated fetch cycles.
- Persist then publish ordering and duplicate-safe replays.
- Dead-letter flow after publish retry exhaustion.
- Health state transitions on dependency outages.
