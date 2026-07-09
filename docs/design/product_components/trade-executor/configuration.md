# TradeExecutor Configuration

Configuration owned by the TradeExecutor process. Values are loaded through the standard env-file
layering (`.env.shared` → `.env.prod` → `.env.trade-executor` → `.env.secrets`).

## Environment Variables

```bash
# Schema ownership / dependencies
TRADE_EXECUTOR_DB_SCHEMA=trade_executor
SHARED_DB_SCHEMA=shared
MARKET_DATA_DB_SCHEMA=market_data

# Trading mode (paper is the default; live requires an explicit override AND a live IBKR port)
TRADE_EXECUTOR_TRADING_MODE=paper      # paper | live

# IBKR connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7497                         # 7497=TWS paper, 4002=Gateway paper, 7496=TWS live, 4001=Gateway live
IBKR_TRADE_EXECUTOR_CLIENT_ID=5        # distinct from MarketData's client id (2)

# Signal consumption (signal_queue Redis stream)
SIGNAL_QUEUE=signal_queue
TRADE_EXECUTOR_CONSUMER_GROUP=trade_executor_group
TRADE_EXECUTOR_CONSUMER_NAME=trade-executor-1
FAILED_MESSAGES_DLQ=failed_messages_dlq
TRADE_EXECUTOR_BATCH_SIZE=16
TRADE_EXECUTOR_BLOCK_MS=5000
TRADE_EXECUTOR_CLAIM_MIN_IDLE_SECONDS=60
TRADE_EXECUTOR_MAX_DELIVERY_ATTEMPTS=5

# Admission gate
TRADE_EXECUTOR_MIN_CONFIDENCE=0.6      # drop cards below this confidence
TRADE_EXECUTOR_QUOTE_MAX_AGE_SECONDS=30  # fail closed if the execution quote is older than this

# Level construction (ATR bracket + R-multiple)
ATR_STOP_MULT=1.5                      # k: stop distance = k * atr_20d
TAKE_PROFIT_R=2.0                      # R: take-profit at R * stop-distance

# Smart limit execution
ENTRY_LIMIT_SLIPPAGE_BPS=5             # marketable-limit buffer past the touch price
ORDER_FILL_TIMEOUT_SECONDS=30          # cancel/re-price the unfilled entry after this
OUTSIDE_RTH=false                      # allow fills outside regular trading hours

# Exit management
TIME_HORIZON_DAYS_MAP=swing_1d_5d=5    # time_horizon -> max holding days (force-flatten at expiry);
                                       # cards with an unmapped horizon are dropped (horizon_unmapped)

# Trading-day boundary (daily counters and kill-switch reset at midnight in this zone)
TRADING_DAY_TIMEZONE=America/New_York

# Portfolio guardrails / kill-switch
MAX_POSITION_SIZE=3000                 # max USD notional per single position
MAX_POSITIONS=5                        # max concurrent open positions
MAX_PORTFOLIO_EXPOSURE=10000           # max total USD across open positions
MAX_SECTOR_EXPOSURE=2500               # max USD per sector (best-effort; skipped if sector unknown)
DAILY_LOSS_LIMIT=200                   # halt new entries when day PnL <= -this
MAX_DAILY_TRADES=10                    # max entries placed per trading day

# Observability
TRADE_EXECUTOR_LOG_LEVEL=INFO
TRADE_EXECUTOR_LOG_FILE=logs/trade-executor.log
```

## Behavior Notes

- **Paper by default; mode and port must agree.** TradeExecutor refuses to **start** when
  `TRADE_EXECUTOR_TRADING_MODE` and `IBKR_PORT` disagree in either direction — `paper` mode with a
  live-account port would silently trade a live account, and `live` mode with a paper port is a
  misconfiguration. Live trading requires both the explicit `live` override and a live port.
- **Fail closed.** If the execution-time IBKR quote is missing/older than
  `TRADE_EXECUTOR_QUOTE_MAX_AGE_SECONDS`, `atr_20d` is unavailable, or the card's `time_horizon` has
  no `TIME_HORIZON_DAYS_MAP` entry, the card is rejected and no order is placed.
- **Confidence gate is uncalibrated.** `TRADE_EXECUTOR_MIN_CONFIDENCE=0.6` is still enforced, but
  current regeneration evidence shows it has not been a binding safety control. Treat it as a
  measured compatibility gate until the Backtester confidence-calibration report has enough closed
  trades to prove whether confidence discriminates outcomes; do not tune it upward or rely on it for
  sizing without that evidence.
- **Bracket orders.** Each entry is submitted with an attached protective stop and take-profit sharing
  an OCA group; `TIME_HORIZON_DAYS_MAP` drives the time-based force-flatten.
- **Shorts allowed.** `direction=sell` cards open short positions; notional caps apply to absolute
  exposure, and borrow/margin constraints are delegated to IBKR at submission.
- **Kill-switch.** When realized + unrealized day PnL breaches `DAILY_LOSS_LIMIT` (or
  `MAX_DAILY_TRADES` is reached), new entries are halted for the remainder of the trading day. The
  halt latches even if PnL recovers; existing positions continue to be managed. The trading day is
  the calendar date in `TRADING_DAY_TIMEZONE`.
- **Single instance.** Portfolio guardrails (position caps, exposure reservation for working orders,
  daily counters) assume exactly one TradeExecutor consumer instance.
- Secrets required for a live IBKR session (if any) belong in `.env.secrets`.

## Shared Dependencies

TradeExecutor also depends on shared PostgreSQL connection, operational, and queue settings defined in
`docs/design/shared/configuration.md`.

TradeExecutor reads cached preliminary market context from the MarketData schema, but must use its own
IBKR connection for final execution-time quote refresh.
