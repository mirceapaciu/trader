# MarketData Configuration

Configuration owned by the MarketData process.

## Environment Variables

```bash
# Database ownership
MARKET_DATA_DB_SCHEMA=market_data

# Provider policy
MARKET_DATA_PRIMARY_PROVIDER=ibkr
MARKET_DATA_BACKFILL_PROVIDER=alpha_vantage
MARKET_DATA_ALLOW_DELAYED=true

# Historical-bars provider policy (used by the Backtester warmup)
# Fallback provider when IBKR is not connected; IBKR is preferred first when a live
# session is available (availability-gated) unless MARKET_DATA_PREFER_IBKR_HISTORICAL=false.
MARKET_DATA_HISTORICAL_BARS_PROVIDER=polygon
MARKET_DATA_PREFER_IBKR_HISTORICAL=true
POLYGON_API_KEY=
POLYGON_MAX_REQUESTS_PER_MINUTE=5

# Freshness and refresh cadence
MARKET_DATA_QUOTE_MAX_AGE_SECONDS=1200
MARKET_DATA_CONTEXT_MAX_AGE_SECONDS=1800
MARKET_DATA_DAILY_BAR_LOOKBACK_DAYS=90
MARKET_DATA_MARKET_HOURS_REFRESH_SECONDS=120
MARKET_DATA_OFF_HOURS_REFRESH_SECONDS=1800

# IBKR market-data connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_MARKET_DATA_CLIENT_ID=2

# Free or low-cost historical backfill
ALPHA_VANTAGE_API_KEY=
```

## Shared Dependencies

MarketData also depends on shared PostgreSQL settings and the shared watchlist table defined in `docs/design/shared/configuration.md`.
