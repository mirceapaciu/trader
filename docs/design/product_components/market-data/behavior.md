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
1. IBKR for current quotes and historical bars when Gateway or TWS is connected.
2. IBKR delayed mode when realtime subscriptions are unavailable and delayed data is permitted.
3. Alpha Vantage for low-frequency daily-bar backfill only when a verified provider mapping exists.

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

## 5. Failure Handling

MarketData must degrade by preserving existing cache rows and writing failed fetch-run records. Provider failures must not cause ThesisBuilder or TradeExecutor to fetch providers directly.

Failures should include concise machine-readable error codes and enough context to inspect affected provider, instrument, and operation.
