# NewsFetcher Data Model

Tables owned by the NewsFetcher component.
PostgreSQL schema: `news_fetcher`.

## Logical Model

### `t_news_articles`

Purpose:
- Durable audit trail of accepted canonical articles.

Logical fields:
- `id` (primary key): deterministic article identity.
- `source`: source/provider identifier.
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

- Executable PostgreSQL DDL is maintained in `src/product-components/news-fetcher/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
