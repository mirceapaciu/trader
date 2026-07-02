# Backtester Configuration

Configuration owned by the Backtester process.

## Environment Variables

```bash
# Database ownership
BACKTESTER_DB_SCHEMA=backtester

# Run execution
BACKTESTER_DEFAULT_TRIGGER_MODE=manual_cli
BACKTESTER_DEFAULT_MODE=replay
BACKTESTER_DEFAULT_CARD_POPULATION=all
BACKTESTER_REQUIRE_TIME_WINDOW=true
BACKTESTER_DEFAULT_LOOKBACK_DAYS=90
BACKTESTER_RUN_TIMEOUT_SECONDS=3600

# Timing model (ideal vs actual entry, delay reporting)
BACKTESTER_DEFAULT_TIMING_SCENARIO=ideal
BACKTESTER_IDEAL_FETCH_DELAY_SECONDS=120
BACKTESTER_IDEAL_THESIS_DELAY_SECONDS=60
BACKTESTER_DELAY_BUCKETS_SECONDS=300,900,3600,14400

# Capital and benchmark
BACKTESTER_INITIAL_CAPITAL_USD=10000
BACKTESTER_RISK_FREE_RATE_ANNUAL=0.02
BACKTESTER_SHARPE_PERIODICITY=daily

# Execution model (fill simulation)
BACKTESTER_BAR_INTERVAL=1m
BACKTESTER_ENTRY_SLIPPAGE_BPS=5
BACKTESTER_EXIT_SLIPPAGE_BPS=5
BACKTESTER_COMMISSION_MODEL=per_share
BACKTESTER_COMMISSION_PER_SHARE_USD=0.005
BACKTESTER_COMMISSION_MIN_USD=1.0
BACKTESTER_LIMIT_ORDER_VALIDITY_BARS=3
BACKTESTER_INTRABAR_STOP_BEFORE_TARGET=true

# Risk and sizing model (mirrors live risk manager defaults)
BACKTESTER_MAX_POSITION_USD=1000
BACKTESTER_MAX_PORTFOLIO_EXPOSURE_USD=5000
BACKTESTER_MAX_POSITIONS_PER_SECTOR=3
BACKTESTER_MAX_DAILY_TRADES=10
BACKTESTER_DAILY_LOSS_LIMIT_USD=200
BACKTESTER_TICKER_COOLDOWN_MINUTES=30

# Regeneration mode (LLM replay of ThesisBuilder)
BACKTESTER_REGENERATION_ENABLED=false
BACKTESTER_LLM_MODEL=gpt-4o-mini
BACKTESTER_LLM_MAX_TOKENS_PER_RUN=1500000
BACKTESTER_LLM_CONCURRENCY=4

# Output policy
BACKTESTER_PERSIST_EQUITY_POINTS=true
BACKTESTER_PERSIST_CARD_SNAPSHOTS=true

# Operations
BACKTESTER_LOG_LEVEL=INFO
```

## Settings Notes

- Execution-model and risk-model variables are defaults only. Each value may be overridden per run
  through `execution_model_snapshot_json` and `risk_model_snapshot_json` in the trigger payload, and
  the effective values are persisted immutably on the run row.
- `BACKTESTER_DEFAULT_MODE=regeneration` requires `BACKTESTER_REGENERATION_ENABLED=true`; otherwise
  the run fails closed before any LLM call.
- `BACKTESTER_BAR_INTERVAL` selects the historical bar granularity requested from MarketData. The
  default and required minimum resolution is 1-minute (`1m`) so entries and exits are simulated
  accurately; MarketData fetches missing ranges on demand and stores them durably for reuse.
- `BACKTESTER_DEFAULT_TIMING_SCENARIO` selects ideal/actual/both entry timing; `both` runs the
  actual-timing simulation alongside ideal and reports the latency P&L gap.
- `BACKTESTER_IDEAL_FETCH_DELAY_SECONDS` and `BACKTESTER_IDEAL_THESIS_DELAY_SECONDS` define the
  feasible always-on pipeline delay added to news publication to compute the ideal entry time; they
  can be overridden per run.
- `BACKTESTER_DELAY_BUCKETS_SECONDS` is a comma-separated list of bucket edges used to group trades by
  measured pipeline delay in reports.

## Shared Dependencies

The Backtester also depends on shared PostgreSQL connection and operational settings defined in
`docs/design/shared/configuration.md`.

## Ownership Rules

- Variables prefixed with `BACKTESTER_` are owned by the Backtester.
- ThesisBuilder and MarketData variables remain owned by their respective configuration specs; the
  Backtester consumes those components only through their documented read contracts.
- Shared cross-process variables belong in `docs/design/shared/configuration.md`.
