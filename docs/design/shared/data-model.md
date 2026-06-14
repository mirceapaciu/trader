# Shared Data Model

Cross-cutting tables used by multiple components.
PostgreSQL schema: `shared`.

## Logical Model

### `t_thesis_card_reviews`

Purpose:
- Durable review state for thesis cards as approved/rejected by user or policy.

Logical fields:
- `card_id` (primary key): thesis card identity from ThesisBuilder.
- `decision_state`: `approved` or `rejected`.
- `reviewed_by`: user id or system policy actor.
- `review_reason`: optional explanation for approval/rejection.
- `reviewed_at`: review timestamp.

Behavioral constraints:
- Only one active review state per `card_id`.
- Transition from `rejected` to `approved` requires a new `reviewed_at` and reason.
- TradeExecutor must treat missing review state as `rejected`.

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

### `t_instruments`

Purpose:
- Shared instrument identity and reusable metadata.
- Provides a canonical place for names and identifiers that should not be tied to one news provider.

Logical fields:
- `ticker`: application ticker symbol.
- `exchange_code`: market/exchange identifier.
- `display_name`: optional human-readable instrument name.
- `identifiers`: optional JSON identifiers such as ISIN.
- `is_enabled`: controls whether the instrument metadata is active.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Composite uniqueness on (`ticker`, `exchange_code`).
- Provider-specific symbols do not belong here.

### `t_instrument_aliases`

Purpose:
- Reusable text terms for attributing unstructured news to instruments.

Logical fields:
- `ticker`: application ticker symbol.
- `exchange_code`: market/exchange identifier.
- `alias`: text term such as `Rheinmetall`, `RHM`, or an ISIN.
- `alias_type`: `alias`, `name`, or `identifier`.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Aliases are source-agnostic.
- RSS provider-specific symbols remain in `news_fetcher.t_rss_symbol_rules`.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/shared/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
