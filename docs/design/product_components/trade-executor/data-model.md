# TradeExecutor Data Model

Tables owned by the TradeExecutor component.
PostgreSQL schema: `trade_executor`.

## Logical Model

### `t_trade_decisions`

Purpose:
- Durable record of risk-evaluated trade decisions produced before broker execution.

Logical fields:
- `id` (primary key): decision record identity.
- `thesis_card_id`: required reference to analyzer thesis card identity.
- `ticker`: target instrument symbol.
- `exchange_code`: target exchange identifier used for market-data and broker routing.
- `action`: intended action (`buy`, `sell`, `hold`).
- `quantity`: intended order quantity (risk-based; see sizing below).
- `order_type`: order type (`market`, `limit`, or policy-defined extension).
- `limit_price`: optional limit price when order type requires it.
- `entry_price`: reference execution price used for level construction and sizing.
- `stop_price`: protective stop level (`entry ∓ ATR_STOP_MULT · atr_20d`).
- `take_profit_price`: take-profit level at `TAKE_PROFIT_R` reward-to-risk multiple of the stop distance.
- `atr_20d`: 20-day ATR from the MarketData context snapshot used to scale the stop.
- `risk_amount_usd`: intended per-trade dollar risk (from the card's `risk_box.max_loss_usd`).
- `confidence`: thesis-card confidence at decision time.
- `signal_strength`: optional normalized signal strength.
- `source_analysis_ids`: references to analysis evidence used for the decision.
- `risk_check_passed`: final risk gate result.
- `risk_check_details`: optional risk rationale, including machine-readable rejection reason codes —
  admission-gate drops (for example `direction_hold`, `card_expired`, `below_min_confidence`,
  `not_in_watchlist`, `position_exists`, `duplicate_card`, `review_not_approved`,
  `horizon_unmapped`), risk-gate rejections (for example `quote_unavailable`, `atr_unavailable`,
  `size_below_one_share`, `portfolio_cap_exceeded`, `daily_loss_halt`), and reconciliation outcomes
  (`decision_orphaned`).
- `decided_at`: decision timestamp.

Sizing and levels:
- Quantity is risk-based off the card: `quantity = floor(risk_amount_usd / |entry_price − stop_price|)`,
  clamped to the per-position and portfolio exposure caps; a result below one share is rejected.
- Stop and take-profit are derived from `atr_20d` and the configured `ATR_STOP_MULT` / `TAKE_PROFIT_R`;
  they are never taken from the card, which supplies only a textual stop condition.

Behavioral constraints:
- Decision records are append-oriented for auditability.
- Every consumed card leaves exactly one decision row: admission-gate drops are persisted with
  `risk_check_passed = false` and a reason code, not just logged.
- Decisions must capture risk gate outcome before execution attempt, whether the gate passes or fails.
- Instrument identity is the pair (`ticker`, `exchange_code`) for execution routing.
- `thesis_card_id` is mandatory and **UNIQUE** across decisions — the uniqueness constraint is the
  idempotency guard against signal redelivery, so a redelivery race cannot create a second decision
  for the same card.
- Decision is executable only when shared review state for `thesis_card_id` is `approved`; missing
  review state is treated as rejected. Review approvals do not expire — card staleness is governed
  by the card's own `expires_at`.
- A passed decision with no execution rows and no matching broker order after restart is closed out
  as `decision_orphaned` by startup reconciliation and never auto-resubmitted.

### `t_trade_executions`

Purpose:
- Durable record of broker execution lifecycle for each decision.

A single decision produces a bracket of three legs; each leg is one execution row.

Logical fields:
- `id` (primary key): execution record identity.
- `decision_id`: owning trade decision identity.
- `leg_role`: bracket leg role (`entry`, `stop`, `take_profit`).
- `ibkr_order_id`: optional broker-native order identifier.
- `ibkr_oca_group`: OCA group tag shared by the stop and take-profit legs so a fill on one cancels the other.
- `status`: execution status (`submitted`, `filled`, `partial`, `rejected`, `cancelled`).
- `fill_price`: optional average fill price.
- `fill_quantity`: optional filled quantity.
- `commission`: optional total commission.
- `executed_at`: optional execution timestamp.
- `error_message`: optional execution/broker error detail.

Behavioral constraints:
- `decision_id` must reference an existing trade decision.
- Execution status transitions must be captured as durable updates or append events according to implementation policy.
- Every execution attempt must remain traceable to exactly one thesis card through its decision row.

### `t_positions`

Purpose:
- Durable state of each opened position across its lifecycle, used for portfolio exposure caps,
  time-based exits, and PnL/kill-switch accounting.

Logical fields:
- `id` (primary key): position identity.
- `thesis_card_id`: originating thesis card.
- `decision_id`: originating trade decision.
- `ticker`, `exchange_code`: canonical instrument identity.
- `side`: `long` or `short`.
- `quantity`: current open quantity.
- `avg_entry_price`: average fill price of the entry leg.
- `stop_price`, `take_profit_price`: active protective levels.
- `time_exit_at`: timestamp at which the position is force-flattened (from `time_horizon`).
- `opened_at`, `closed_at`: lifecycle timestamps.
- `realized_pnl`: realized profit/loss once (partially) closed.
- `exit_reason`: `stop`, `take_profit`, `time`, `manual`, or `invalidation`.

Behavioral constraints:
- At most one open position per (`ticker`, `exchange_code`) at a time (one-position-per-instrument rule).
- Both `long` and `short` positions are supported (`sell` cards open shorts); all notional caps
  apply to absolute exposure.
- Submitted-but-unfilled entry orders count toward the position-count and portfolio-exposure caps
  as reserved notional, so concurrent admissions cannot collectively breach a cap.
- Position state must be reconcilable against the broker on startup.
- Sector for per-sector exposure caps is derived best-effort from `shared.t_instruments.identifiers`;
  when sector is unknown, the per-sector cap is skipped rather than blocking the trade.

### `t_daily_risk`

Purpose:
- Per-trading-day risk accounting backing the daily-loss kill-switch and daily-trade limit.

Logical fields:
- `trade_date` (primary key): trading day — the calendar date in `TRADING_DAY_TIMEZONE`
  (default `America/New_York`).
- `realized_pnl`: realized profit/loss accumulated for the day.
- `trades_count`: number of entries placed during the day.
- `halted`: whether new entries are halted for the day (kill-switch tripped).

Behavioral constraints:
- New entries are blocked when `realized + unrealized` day PnL breaches the configured daily-loss
  limit or when `trades_count` reaches the daily-trade limit. Only realized PnL is persisted here;
  unrealized PnL is marked from live IBKR portfolio updates at gate-evaluation time and is not
  stored.
- `halted` **latches**: once tripped it remains set for the remainder of the trading day even if
  PnL subsequently recovers. Counters and the halt reset at the `trade_date` boundary.
- Management of existing positions continues even while new entries are halted.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/trade_executor/db/schema.sql`
  and must be updated to match this logical model (adds `thesis_card_id` with a UNIQUE constraint,
  `exchange_code`, and price-level/risk fields to `t_trade_decisions`; bracket-leg fields to
  `t_trade_executions`; the `t_positions` table with an index supporting the open-position lookup
  per (`ticker`, `exchange_code`); and the `t_daily_risk` table).
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
