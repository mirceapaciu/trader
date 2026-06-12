# NewsFetcher Data Model

Tables owned by the NewsFetcher component.
PostgreSQL schema: `news_fetcher`.

## Logical Model

### `t_input_news_articles`

Purpose:
- Durable 30-day normalized input corpus for production filtering and offline filter simulation.

Logical fields:
- `id` (primary key): deterministic article identity.
- `source`: source/provider identifier.
- `source_key`: stable fetch-stream identifier, such as `finnhub` or `rss:marketwatch:marketpulse`.
- `source_event_id`: optional provider-native event id.
- `headline`: normalized article title.
- `summary`: optional short content summary.
- `url`: canonical source URL.
- `tickers`: normalized ticker list.
- `published_at`: source publication timestamp.
- `fetched_at`: ingestion timestamp.
- `sentiment_source`: optional provider-supplied sentiment.
- `created_at`: row creation timestamp.

Behavioral constraints:
- Stores all structurally valid normalized candidates before final production filter outcome is applied.
- Retention target is 30 days.
- Acts as the source corpus for simulation re-filtering.

### `t_news_articles`

Purpose:
- Durable audit trail of accepted canonical articles.

Logical fields:
- `id` (primary key): deterministic article identity and foreign-key-compatible identifier from `t_input_news_articles`.
- `source`: source/provider identifier.
- `source_key`: stable fetch-stream identifier that accepted the article.
- `headline`: normalized article title.
- `summary`: optional short content summary.
- `url`: canonical source URL.
- `tickers`: normalized ticker list.
- `published_at`: source publication timestamp.
- `fetched_at`: ingestion timestamp.
- `sentiment_source`: optional provider-supplied sentiment.

Behavioral constraints:
- Idempotent upsert by `id`.
- Preserve first-seen ingestion semantics during retries/replays.
- Contains only the accepted subset of `t_input_news_articles`.

### `t_news_filter_runs`

Purpose:
- Durable metadata for one filter execution context.

Logical fields:
- `filter_run_id` (primary key): stable filter-run identity.
- `run_mode`: `production` or `simulation`.
- `filter_config_fingerprint`: deterministic full-context fingerprint for the filter configuration.
- `filter_config_snapshot_json`: immutable effective filter configuration used for this run; populated for simulation runs and optional for production runs when a full snapshot is persisted.
- `run_note`: optional operator note.
- `created_at`: creation timestamp.
- `last_used_at`: last execution timestamp for this run context.

Behavioral constraints:
- Production runs represent baseline writes from NewsFetcher.
- Simulation runs are created by Filter Quality Evaluator and do not mutate production state.
- One production run row may be reused for the same fingerprint.
- Simulation configuration must be applied only to the simulation run identified by `filter_run_id`; NewsFetcher must not read simulation overrides from global environment variables.
- The effective filter configuration for a simulation run is the immutable JSON snapshot stored with that run.
- Production execution continues to read the active production configuration source and is not affected by simulation snapshots.

### `t_news_filter_results`

Purpose:
- One production or simulation filter outcome per article and filter run.

Logical fields:
- `filter_run_id`: parent filter run identity.
- `article_id`: candidate article identity from `t_input_news_articles`.
- `filter_outcome`: `accepted` or `rejected`.
- `rejection_reason_code`: nullable machine-readable reason when rejected.
- `matched_article_id`: nullable accepted-article id for dedupe rejections.
- `similarity_score`: nullable dedupe similarity score.
- `details_json`: structured audit metadata.
- `created_at`: row creation timestamp.

Behavioral constraints:
- Primary key is (`filter_run_id`, `article_id`).
- Every row must reference an existing `t_input_news_articles` row.
- Production baseline writes come from NewsFetcher.
- Simulation writes come from Filter Quality Evaluator and must not alter production baseline rows.

### `t_source_checkpoints`

Purpose:
- Durable per-source incremental progress for replay-safe ingestion.

Logical fields:
- `source_key` (primary key): stable source stream identifier.
- `cursor_value`: provider-specific incremental cursor.
- `cursor_updated_at`: event boundary included by cursor.
- `version`: optimistic concurrency version.
- `updated_at`: record update timestamp.

Behavioral constraints:
- Exactly one active checkpoint per `source_key`.
- Checkpoint advancement uses optimistic concurrency.
- Checkpoint advances only after persistence and publish obligations are completed.

### `t_provider_cycle_status`

Purpose:
- Last-cycle heartbeat and summary for each provider/source key.
- Provides liveness telemetry even when a cycle fetches zero accepted articles and does not advance a checkpoint.

Logical fields:
- `source_key` (primary key): stable source stream identifier.
- `last_cycle_started_at`: UTC timestamp when the most recent cycle started.
- `last_cycle_finished_at`: UTC timestamp when the most recent cycle finished.
- `last_non_zero_fetch_at`: UTC timestamp when the most recent cycle returned one or more source events.
- `last_cycle_status`: `success` or `error`.
- `last_cycle_error_code`: nullable machine-readable failure reason.
- `last_cycle_fetched_count`: number of source events returned after provider normalization and relevance filtering.
- `last_cycle_accepted_count`: number of events accepted after deduplication.
- `last_cycle_rejected_count`: number of events rejected after deduplication.
- `last_cycle_checkpoint_advanced`: whether the cycle advanced the source checkpoint.
- `updated_at`: row update timestamp.

Behavioral constraints:
- Exactly one active status row per `source_key`.
- Row must update on every provider cycle, including empty cycles and provider failures.
- `last_non_zero_fetch_at` only advances when `last_cycle_fetched_count > 0`.
- Monitoring UI liveness must use `last_cycle_finished_at`, not checkpoint timestamps.

### `t_rss_sources`

Purpose:
- Provider-level RSS URL generation rules.
- Stores one source template per RSS provider, such as Yahoo Finance.

Logical fields:
- `source_key` (primary key): provider identifier, for example `yahoo_finance`.
- `base_url`: RSS endpoint without ticker-specific query parameters, or exact URL for static RSS sources.
- `source_type`: `static` or `dynamic_watchlist`.
- `symbol_param`: query parameter name used for provider symbols, for example `s`.
- `default_query_params`: provider default query parameters such as region and language.
- `max_symbols_per_request`: maximum provider symbols to combine into one request.
- `min_request_interval_seconds`: minimum interval between requests for generated feeds from this source.
- `grouping_mode`: `grouped`, `single`, or `static`.
- `is_enabled`: controls whether this RSS source generates feed requests.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Enabled grouped sources generate RSS feed specs by batching active watchlist rows.
- Enabled single-symbol sources generate one RSS feed spec per active watchlist row.
- Enabled static sources generate one RSS feed spec per row and do not depend on watchlist rows.
- Source defaults can be overridden per symbol rule.

### `t_rss_symbol_rules`

Purpose:
- Maps app watchlist symbols to provider-specific RSS query symbols.
- Handles cases where Yahoo Finance requires symbols such as `RHM.DE` or `6758.T`.

Logical fields:
- `source_key`: RSS source provider key.
- `ticker`: app watchlist ticker.
- `exchange_code`: app watchlist exchange code.
- `provider_symbol`: provider-specific symbol to place in the RSS query.
- `query_params`: optional provider query parameter overrides.
- `match_terms`: deprecated; reusable attribution terms live in shared instrument aliases.
- `is_enabled`: controls whether the override is active.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Primary key is (`source_key`, `ticker`, `exchange_code`).
- Disabled rules are ignored and the source falls back to the app watchlist ticker.
- Generated Yahoo grouped source keys use `rss:yahoo_finance:batch:<hash>`.
- Generated Yahoo single-symbol source keys use `rss:yahoo_finance:<ticker>:<exchange_code>`.

### `t_publication_obligations`

Purpose:
- Durable transactional outbox for broker publication and checkpoint gating.

Logical fields:
- `obligation_id` (primary key): stable unique obligation identity.
- `source_key`: source stream identifier that owns the obligation.
- `batch_id`: ingestion batch identifier used for checkpoint gate evaluation.
- `canonical_event_id`: canonical article identity to publish.
- `event_type`: semantic event type.
- `dedupe_key`: deterministic downstream idempotency key.
- `envelope_json`: full event envelope payload.
- `status`: obligation status (`pending`, `publishing`, `published`, `dead_lettered`).
- `attempt_count`: publish attempt counter.
- `last_error_code`: last machine-readable publication error code.
- `claimed_by`: publishing worker id owning the current lease.
- `claim_expires_at`: UTC lease expiration for current claim.
- `created_at`: record creation timestamp.
- `updated_at`: record update timestamp.

Behavioral constraints:
- Idempotent upsert must not create duplicate obligations for the same semantic publish action.
- Status transitions must follow: `pending -> publishing -> (published | dead_lettered)`.
- Only non-terminal rows (`pending`, `publishing`) participate in checkpoint blocking.
- Claims must be lease-based and exclusive while lease is valid.
- Terminal updates must clear claim fields.

Physical constraints (required):
- Primary key: `obligation_id`.
- Uniqueness: one obligation per (`canonical_event_id`, `event_type`, `dedupe_key`).
- Foreign key: `canonical_event_id -> t_news_articles.id`.
- Check constraints:
	- `status` in (`pending`, `publishing`, `published`, `dead_lettered`),
	- `attempt_count >= 0`,
	- claim consistency (`claimed_by` and `claim_expires_at` are both null or both non-null),
	- terminal rows must not retain claim fields.
- Required indexes:
	- unresolved-by-source scan (`source_key`, `status`, `updated_at`, `obligation_id`),
	- deterministic claim scan (`status`, `updated_at`, `obligation_id`),
	- stale lease takeover scan (`status`, `claim_expires_at`, `updated_at`, `obligation_id`),
	- batch gate scan (`batch_id`, `status`).

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/news_fetcher/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
