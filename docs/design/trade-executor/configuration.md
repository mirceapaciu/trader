# TradeExecutor Configuration

Configuration owned by the TradeExecutor process.

## Environment Variables

```bash
# Database ownership
TRADEEXECUTOR_DB_SCHEMA=trade_executor

# IBKR connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7497               # 7497=TWS paper, 7496=TWS live, 4002=Gateway paper, 4001=Gateway live
IBKR_CLIENT_ID=1

# Trading parameters
WATCHLIST=AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META
MAX_POSITION_SIZE=1000       # Max USD per single position
MAX_DAILY_TRADES=10
MAX_PORTFOLIO_EXPOSURE=5000  # Max total USD in open positions
DAILY_LOSS_LIMIT=200         # Stop trading if daily loss exceeds this
```

## Shared Dependencies

TradeExecutor also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.
