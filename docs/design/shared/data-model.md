# Shared Data Model

Cross-cutting tables used by multiple components.
PostgreSQL schema: `shared`.

## Logical Model

### `t_api_usage`

Purpose:
- Cross-component accounting of external provider/API usage and estimated cost.

Logical fields:
- `id` (primary key): usage record identity.
- `provider`: upstream provider identifier.
- `endpoint`: provider endpoint or operation name.
- `tokens_used`: optional token usage for metered APIs.
- `cost_estimate`: optional estimated cost for the call.
- `called_at`: timestamp when the provider call occurred.

Behavioral constraints:
- Must be append-only for auditability.
- Nullable metering fields are allowed when a source does not expose token or cost metrics.

### `t_watchlist_tickers`

Purpose:
- Shared watchlist universe used by ingestion and downstream components for relevance filtering.

Logical fields:
- `ticker`: instrument symbol.
- `exchange_code`: market/exchange identifier.
- `is_active`: controls whether ticker is currently considered for filtering.
- `source`: watchlist entry provenance.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Composite uniqueness on (`ticker`, `exchange_code`).
- Consumers must treat only active rows as eligible watchlist membership.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product-components/shared/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
