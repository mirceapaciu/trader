# MarketData Data Model

Tables owned by the MarketData component.
PostgreSQL schema: `market_data`.

## Logical Model

### `t_market_provider_symbols`

Purpose:
- Maps canonical instrument identity to provider-specific market-data symbols and contract metadata.

Logical fields:
- `ticker`: canonical instrument symbol.
- `exchange_code`: canonical MIC exchange code, such as `XNAS`, `XNYS`, `XETR`, or `XPAR`.
- `provider`: source identifier such as `ibkr` or `alpha_vantage`.
- `provider_symbol`: provider-specific symbol.
- `currency`: trading currency.
- `asset_class`: asset class, initially `stock` or `etf`.
- `provider_metadata`: provider-specific contract details, such as IBKR primary exchange or conid.
- `is_verified`: whether this mapping has been verified.
- `is_enabled`: whether this mapping may be used.
- `created_at`, `updated_at`: timestamps.

Behavioral constraints:
- Canonical identity remains (`ticker`, `exchange_code`); provider symbols must not replace it.
- MarketData must skip external fetches for provider mappings that are disabled or unverified.
- Existing non-MIC codes such as `ETR` should be migrated to canonical MIC codes such as `XETR`.

### `t_market_quotes`

Purpose:
- Latest normalized quote snapshots from market-data providers.

Logical fields:
- `ticker`, `exchange_code`: canonical instrument identity.
- `provider`: quote source.
- `data_type`: `realtime`, `delayed`, `frozen`, `stale`, or `missing`.
- `currency`: quote currency.
- `bid_price`, `ask_price`, `last_price`, `previous_close`, `volume`: quote fields.
- `provider_timestamp`: timestamp attached by the provider when available.
- `fetched_at`: local fetch timestamp.
- `provider_metadata`: raw provider status and diagnostic details.

Behavioral constraints:
- One latest quote is kept per instrument/provider/data type.
- Missing or stale quote status is persisted for auditability and consumer fail-closed decisions.

### `t_market_bars`

Purpose:
- Normalized OHLCV bar cache for current and historical market context.

Logical fields:
- `ticker`, `exchange_code`: canonical instrument identity.
- `provider`: source provider.
- `bar_interval`: interval such as `1d`, `1h`, or `5m`.
- `bar_start_at`: bar start timestamp.
- `currency`: bar currency.
- `open_price`, `high_price`, `low_price`, `close_price`, `volume`: OHLCV fields.
- `adjusted`: whether prices are adjusted for splits/dividends.
- `fetched_at`: local fetch timestamp.
- `provider_metadata`: raw provider status and diagnostic details.

Behavioral constraints:
- Daily bars are the minimum required interval for ThesisBuilder market context.
- Intraday bars, including 1-minute, are durably retained when fetched (for example to serve the
  Backtester historical-bars API).
- This table is a permanent, reusable store, not a request cache: a bar identified by its primary key
  (`ticker`, `exchange_code`, `provider`, `bar_interval`, `bar_start_at`, `adjusted`) is never evicted
  or expired, and is immutable once written except for adding a distinct `adjusted` variant.
- Bars are served to consumers only through the MarketData historical-bars read API
  (`get_historical_bars`), which backfills missing ranges on demand; consumers must not read this
  table directly.

### `t_market_context_snapshots`

Purpose:
- Derived market context consumed by ThesisBuilder and TradeExecutor.

Logical fields:
- `ticker`, `exchange_code`: canonical instrument identity.
- `as_of`: context timestamp.
- `source_status`: `fresh`, `delayed`, `stale`, or `missing`.
- `current_price`, `previous_close`: latest usable prices.
- `return_1d`, `return_5d`, `return_20d`: recent returns.
- `atr_20d`, `volatility_20d`: volatility measures.
- `volume_ratio_20d`: latest volume versus 20-day average.
- `sma_20d`, `sma_50d`: moving averages.
- `recent_high_20d`, `recent_low_20d`, `drawdown_from_high_20d`: recent range context.
- `quote_fetched_at`, `bars_fetched_at`: source freshness timestamps.
- `created_at`: snapshot creation timestamp.

Behavioral constraints:
- Consumers must evaluate source freshness before using a context snapshot.
- ThesisBuilder must fail closed for strategies that require market context when the snapshot is stale or missing.

### `t_instrument_fundamentals`

Purpose:
- Point-in-time cache of slow-moving company fundamentals used to judge the scale of a news event
  relative to company size. Served through the `get_fundamentals` / `get_fundamentals_as_of` API.

Logical fields:
- `id`: primary key.
- `ticker`, `exchange_code`: canonical instrument identity.
- `provider`: source identifier (initially `finnhub`).
- `market_cap_usd`, `shares_outstanding`, `revenue_ttm_usd`: normalized company scale figures in USD/shares.
- `next_earnings_date`: next scheduled earnings date when known.
- `payload_json`: raw provider responses retained for audit and re-derivation.
- `fetched_at`: when this set of values was first observed.
- `last_checked_at`: most recent refresh that confirmed these values unchanged.
- `created_at`: row creation timestamp.

Behavioral constraints:
- Append-only with change detection: a new row is inserted only when a prompt-visible value
  (market cap, shares, revenue, or next earnings date) differs from the latest stored row; an
  unchanged refresh only advances `last_checked_at`. A row's validity window is therefore
  `[fetched_at, next row's fetched_at)`, which is what `get_fundamentals_as_of` selects against for
  look-ahead-free regeneration.
- Fundamentals are advisory consumer context; missing coverage yields `None` rather than an error.
- Served only through the MarketData API; consumers must not read this table directly.

### `t_market_data_fetch_runs`

Purpose:
- Operational audit trail for provider calls and refresh attempts.

Logical fields:
- `id`: primary key.
- `provider`, `operation`: external call identity.
- `ticker`, `exchange_code`: optional instrument identity.
- `status`: `success`, `skipped`, or `failed`.
- `error_code`: machine-readable failure reason.
- `started_at`, `finished_at`: timing.
- `fetched_count`: number of normalized records written.
- `details`: provider diagnostics.

Behavioral constraints:
- Fetch runs are append-only.
- All external provider usage should also write a coarse record through the shared API usage adapter/contract, which persists the shared audit record in `shared.t_api_usage`.
