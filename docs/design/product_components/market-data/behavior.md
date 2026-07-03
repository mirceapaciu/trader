# MarketData Behavior Specification

## 1. Purpose and Scope

MarketData is the product component that retrieves, normalizes, and caches current and historical market data for watched instruments.

Responsibilities:
- Load active watchlist instruments through the Shared Instrument Registry API or documented shared watchlist contract.
- Resolve provider-specific symbols and contract metadata.
- Fetch current quotes and historical bars from low-cost providers.
- Cache normalized quotes, bars, and derived market context in PostgreSQL.
- Provide a shared read surface for ThesisBuilder and TradeExecutor.
- Centralize provider pacing, failure handling, and usage accounting.

Out of scope:
- Trading decisions, thesis-card generation, order sizing, and broker order submission.
- News fetching or article attribution.
- Direct UI rendering beyond exposing cached data through future UI adapters.

## 2. Source Policy

Canonical instrument identity uses (`ticker`, `exchange_code`), where `exchange_code` is a MIC code. Initial supported markets:
- US equities and ETFs: `XNAS`, `XNYS`.
- Germany: `XETR`.
- France: `XPAR`.

Provider priority:
1. IBKR for current quotes and historical bars when Gateway or TWS is connected. IBKR is the preferred
   source for intraday historical bars, including 1-minute resolution used by the Backtester.
2. IBKR delayed mode when realtime subscriptions are unavailable and delayed data is permitted.
3. Alpha Vantage for low-frequency daily-bar backfill only when a verified provider mapping exists.

Historical-bars provider selection (US instruments) is availability-gated: when an IBKR session is
live, IBKR is tried first, otherwise MarketData falls back to the configured
`MARKET_DATA_HISTORICAL_BARS_PROVIDER` (Polygon) and then Alpha Vantage. IBKR is only placed first
when actually connected — a disconnected IBKR must never win selection, because a selected provider
records its requested window as covered even when it returns zero bars, which would defeat the
fallback. The `MARKET_DATA_PREFER_IBKR_HISTORICAL` toggle (default `true`) disables the IBKR-first
preference without changing connection settings. Non-US instruments (for example `XETR`, `XPAR`) are
IBKR-only, since the Polygon free tier is US-only. The IBKR session used for historical bars runs on
`IBKR_MARKET_DATA_CLIENT_ID` (default 2), independent of the TradeExecutor's broker session.

Provider-specific symbols such as `RHM.DE`, `AXA.PA`, IBKR contract metadata, or Alpha Vantage symbols are stored in MarketData provider mapping rows.

## 3. Refresh Flow

For each active watched instrument:

1. Load enabled and verified provider mappings.
2. Fetch latest quote from IBKR when an IBKR mapping is available.
3. Classify quote status as `realtime`, `delayed`, `frozen`, `stale`, or `missing`.
4. Fetch recent historical bars from IBKR where available.
5. Backfill missing daily bars from Alpha Vantage when configured.
6. Upsert normalized quotes and bars.
7. Recompute and persist the latest market context snapshot.
8. Record fetch run status and shared API usage.

Default refresh cadence:
- Market hours: every 120 seconds.
- Off hours: every 1800 seconds.

## 4. Consumer Contract

Consumers retrieve market context through the MarketData component API. They must not query MarketData-owned tables directly and must not call external market-data providers directly.

This is the required component database encapsulation boundary for current and historical market data. MarketData tables are private to the MarketData component; consumers receive snapshots through the API and copy those snapshots into their own audit records when needed.

Python API:

```python
get_market_context(ticker: str, exchange_code: str, refresh_if_stale: bool = True) -> MarketContextSnapshot
```

API behavior:
- The returned snapshot uses canonical instrument identity (`ticker`, `exchange_code`).
- When `refresh_if_stale=True`, MarketData may refresh quote and bar data according to provider priority, pacing, delayed-data policy, and configured backfill policy before returning the latest context.
- MarketData owns cache reads, freshness evaluation, provider refresh, provider failure classification, fetch-run records, and provider usage accounting.
- If refresh fails, MarketData returns the best available context with `source_status` reflecting whether it is usable, stale, or missing.
- Callers that need audit stability must copy the returned snapshot into their own records.

ThesisBuilder uses `get_market_context(..., refresh_if_stale=True)` for strategies that require price-derived validation. It must fail closed for those strategies when the returned context remains stale or missing.

TradeExecutor may retrieve cached market context through the MarketData API for preliminary risk validation, but must refresh the final execution quote through IBKR before order placement.

Consumer status rules:
- `fresh` or `delayed`: usable for ThesisBuilder if the strategy allows delayed data.
- `stale` or `missing`: not usable for strategies requiring market context.
- Execution pricing must use a final IBKR quote and must fail closed if the quote is unavailable or too stale.

### 4.1 Historical bars read API

MarketData exposes a historical-bars read surface for consumers that need raw OHLCV series over a
time range, such as the Backtester:

```python
get_historical_bars(ticker: str, exchange_code: str, interval: str, start: datetime, end: datetime) -> list[MarketBar]
```

API behavior:
- Returns normalized OHLCV bars for the canonical instrument identity (`ticker`, `exchange_code`) at
  the requested `interval` (for example `1m`, `5m`, `1d`), covering `[start, end]`.
- On-demand backfill: if part of the requested range is not already stored, MarketData fetches the
  missing sub-ranges from the provider (IBKR primary for intraday), persists them, then returns the
  full requested range. Consumers never call external providers directly.
- Provider pacing, failure classification, fetch-run records, and shared API usage accounting apply
  exactly as for the rest of MarketData.
- If a sub-range cannot be retrieved, MarketData returns the bars it has and reports the missing
  coverage so callers (for example the Backtester) can skip instruments lacking price history.

### 4.2 Historical bar durability

Historical bars are a permanent, reusable store, not a request cache:
- Once stored, a bar identified by (`ticker`, `exchange_code`, `provider`, `bar_interval`,
  `bar_start_at`, `adjusted`) is durable and is never evicted or expired.
- A stored bar is immutable for that key; re-fetches must not overwrite closed historical bars except
  to add a distinct `adjusted` variant.
- This durability lets repeated backtests reuse previously fetched 1-minute history without
  re-querying providers. The rolling quote and derived-context cache semantics in Sections 3 and 4
  are unchanged; durability applies to the bar store.

## 5. Failure Handling

MarketData must degrade by preserving existing cache rows and writing failed fetch-run records. Provider failures must not cause ThesisBuilder or TradeExecutor to fetch providers directly.

Failures should include concise machine-readable error codes and enough context to inspect affected provider, instrument, and operation.
