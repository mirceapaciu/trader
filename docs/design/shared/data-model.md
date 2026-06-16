# Shared Data Model

Cross-cutting tables used by multiple components.
PostgreSQL schema: `shared`.

## Access Contract

The `shared` schema is the only schema intended for cross-component contracts. It is still not a general-purpose shortcut around component ownership.

Shared tables must be accessed through shared component APIs or documented shared adapters. Product components must not create ad hoc SQL dependencies on shared physical tables unless this file explicitly identifies that access as part of the contract.

Each shared contract must identify:
- The owning contract.
- The allowed writers.
- The allowed readers.
- Whether direct SQL is permitted or whether access must go through an API/adapter.

Component-owned schemas remain private even when shared tables contain ids copied from them. Consumers should copy ids and snapshots needed for audit rather than relying on foreign table joins.

## Logical Model

### `t_thesis_card_reviews`

Purpose:
- Durable review state for thesis cards as approved/rejected by user or policy.

Contract ownership:
- Owner: shared review contract.
- Writers: ThesisBuilder system policy and Monitoring UI review workflow through the shared review API/adapter.
- Readers: TradeExecutor, Monitoring UI, and audit tooling through the shared review API/adapter.
- Direct SQL from unrelated product repositories is not permitted.

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

Contract ownership:
- Owner: shared API usage contract.
- Writers: components that call external providers, through the shared usage API/adapter.
- Readers: Monitoring UI, budget checks, and audit tooling through the shared usage API/adapter.
- Direct SQL from unrelated product repositories is not permitted.

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

Contract ownership:
- Owner: shared Instrument Registry contract.
- Writers: operator/admin workflow and seed/migration tooling.
- Readers: NewsFetcher, MarketData, ThesisBuilder, Monitoring UI, and other consumers through the Instrument Registry API or documented shared adapter.
- Direct SQL from unrelated product repositories is not permitted.

Logical fields:
- `ticker`: instrument symbol.
- `exchange_code`: market/exchange identifier.
- `is_active`: controls whether ticker is currently considered for filtering.
- `source`: watchlist entry provenance.
- `created_at`: creation timestamp.
- `updated_at`: update timestamp.

Behavioral constraints:
- Composite uniqueness on (`ticker`, `exchange_code`).
- The registry contract must expose only active rows as eligible watchlist membership unless a caller explicitly requests inactive entries for audit.

### `t_instruments`

Purpose:
- Shared instrument identity and reusable metadata.
- Provides a canonical place for names and identifiers that should not be tied to one news provider.

Contract ownership:
- Owner: shared Instrument Registry contract.
- Writers: operator/admin workflow and seed/migration tooling.
- Readers: NewsFetcher, MarketData, ThesisBuilder, Monitoring UI, and other consumers through the Instrument Registry API or documented shared adapter.
- Direct SQL from unrelated product repositories is not permitted.

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

Contract ownership:
- Owner: shared Instrument Registry contract.
- Writers: operator/admin workflow and seed/migration tooling.
- Readers: NewsFetcher and ThesisBuilder through the Instrument Registry API or documented shared adapter.
- Direct SQL from unrelated product repositories is not permitted.

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

### `t_instrument_lookup_cache`

Purpose:
- Non-canonical cache for external instrument search and alias-discovery responses used by operator/admin workflows.

Contract ownership:
- Owner: shared Instrument Registry contract.
- Writers: shared-owned instrument-registry/admin lookup service.
- Readers: shared-owned instrument-registry/admin lookup service.
- Product components must not treat this table as a source of truth for watchlist membership or instrument metadata.

Logical fields:
- `operation`: cache operation such as `search` or `alias_discovery`.
- `target`: normalized cache key target such as a search string or `ticker|exchange_code`.
- `provider`: external lookup provider used to produce the cached payload.
- `payload_json`: normalized provider result snapshot.
- `fetched_at`: timestamp when the provider result was fetched.
- `expires_at`: timestamp after which the cached payload must be considered stale.

Behavioral constraints:
- Canonical watchlist and alias state remain in `t_watchlist_tickers`, `t_instruments`, and `t_instrument_aliases`.
- Stale cache rows may be refreshed or ignored; they must not silently override canonical data.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/shared/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
