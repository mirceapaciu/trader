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

## Notes

- Executable PostgreSQL DDL is maintained in `src/product-components/news-fetcher/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
