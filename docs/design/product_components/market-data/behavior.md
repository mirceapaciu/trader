# MarketData Behavior Specification

## 1. Purpose and Scope

MarketData is the product component that retrieves, normalizes, and caches current and historical market data for watched instruments.

Responsibilities:
- Load active watchlist instruments from shared tables.
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

ThesisBuilder reads cached market context only. It must not call external market-data providers directly.

TradeExecutor may read cached market context for preliminary risk validation, but must refresh the final execution quote through IBKR before order placement.

Consumer status rules:
- `fresh` or `delayed`: usable for ThesisBuilder if the strategy allows delayed data.
- `stale` or `missing`: not usable for strategies requiring market context.
- Execution pricing must use a final IBKR quote and must fail closed if the quote is unavailable or too stale.

## 5. Failure Handling

MarketData must degrade by preserving existing cache rows and writing failed fetch-run records. Provider failures must not cause ThesisBuilder or TradeExecutor to fetch providers directly.

Failures should include concise machine-readable error codes and enough context to inspect affected provider, instrument, and operation.
