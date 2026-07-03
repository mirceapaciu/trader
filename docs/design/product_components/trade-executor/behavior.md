# TradeExecutor Behavior Specification

## 1. Purpose and Scope

TradeExecutor is the terminal product component of the pipeline
(`NewsFetcher → ThesisBuilder → signal_queue → TradeExecutor`). It consumes thesis-card
signals and turns each admissible card into a risk-sized, professionally-managed order on
Interactive Brokers (IBKR).

Responsibilities:
- Consume `thesis_card.created` signals from the `signal_queue` Redis stream.
- Admit or reject each card through a deterministic gate (confidence, expiry, watchlist, dedupe,
  shared review approval).
- Derive concrete execution levels (entry, stop, take-profit) from a fresh IBKR quote and cached
  MarketData context.
- Size positions from the card's stated risk budget.
- Enforce portfolio-level risk guardrails and a daily-loss kill-switch before any order is placed.
- Submit and manage IBKR bracket orders (entry + protective stop + take-profit) and force a
  time-based exit at the end of the card's horizon.
- Keep a durable, auditable trail linking every order and position back to exactly one thesis card.

Out of scope:
- Thesis-card generation, news fetching, and article attribution.
- Fetching market data from external providers directly. TradeExecutor reads cached market context
  through the MarketData component API only; it opens its own IBKR session solely for
  execution-time quotes and order placement.
- UI rendering beyond exposing its own tables through the MonitoringUI read surface.

Safety posture:
- **Paper trading is the default.** Live trading requires an explicit configuration override
  (`TRADE_EXECUTOR_TRADING_MODE=live`) and a live-account IBKR port.
- **Fail closed.** When a required execution quote or volatility input is missing or too stale, the
  component records a rejected decision and does not trade.
- A global daily-loss kill-switch halts all new entries.

## 2. Inputs and Contracts

Canonical instrument identity is the pair (`ticker`, `exchange_code`), where `exchange_code` is a
MIC code, consistent with the rest of the system.

### 2.1 Signal input (`signal_queue`)

TradeExecutor is a consumer-group reader of the `signal_queue` Redis stream. Messages carry the
standard event envelope (`event_id`, `event_type`, `event_version`, `occurred_at`, `producer`,
`dedupe_key`, `payload`). Only `event_type = thesis_card.created` is processed; other event
types are acknowledged and ignored.

The decoded `payload` is the thesis-card signal:

- `thesis_card_id`, `ticker`, `exchange_code`
- `direction` (`buy`, `sell`, `hold`)
- `time_horizon` (for example `swing_1d_5d`)
- `strategy`
- `confidence` (0.0–1.0)
- `risk_box`: `{ max_loss_usd, stop_condition, invalidation_condition }`
- `source_analysis_ids`
- `created_at`, `expires_at`

The card provides only a **textual** stop condition (for example
`close_below_recent_support`), not concrete prices. TradeExecutor is responsible for converting the
card's risk budget into concrete entry, stop, and take-profit prices (Section 3).

### 2.2 MarketData context (read surface)

TradeExecutor retrieves cached market context through the MarketData component API and must not
query MarketData-owned tables or external providers directly:

```python
get_market_context(ticker: str, exchange_code: str, refresh_if_stale: bool = True) -> MarketContextSnapshot
```

It uses `atr_20d` for level construction and `recent_low_20d` / `recent_high_20d` /
`current_price` for sanity checks. Per `market-data/behavior.md` §4, this cached context is for
**preliminary** risk validation only; final execution pricing must come from a fresh IBKR quote.

### 2.3 Shared review state

A decision is executable only when the shared review for `thesis_card_id` is `approved`.
ThesisBuilder writes a system-preapproved review when it publishes the signal. TradeExecutor reads
review state through the shared review contract and fails closed when review state is missing or
not `approved` (missing state is treated as rejected). Review approvals do not expire; card
staleness is governed solely by the card's own `expires_at` in the admission gate.

### 2.4 IBKR connection

TradeExecutor opens its **own** IBKR session (distinct client id from MarketData) for:
- an execution-time quote refresh immediately before placing an order, and
- order submission, order-status/fill callbacks, and position/order reconciliation.

The connection points at a paper account by default (Section 5).

## 3. Processing Pipeline

Each stream message is processed through the stages below. A message is acknowledged
(`XACK`) once it reaches a terminal outcome — admitted-and-submitted, deliberately dropped, or
recorded as a failed decision. Only transient infrastructure errors leave a message unacknowledged
for retry, and a message that exceeds `max_delivery_attempts` is routed to the dead-letter queue.

### 3.1 Consume

Consumer-group semantics mirror ThesisBuilder's stream reader:
1. Read this consumer's pending entries first (`XREADGROUP` at id `0`).
2. Reclaim entries idle beyond `claim_min_idle_seconds` from dead consumers (`XAUTOCLAIM`).
3. Read new entries (`XREADGROUP` at id `>`), blocking up to `block_ms`.

The consumer group is created if absent (`mkstream`), and group/consumer names are configurable.

### 3.2 Admission gate (dedupe and confidence)

A card is dropped when any of the following hold. Every drop is acknowledged and persisted as a
`t_trade_decisions` row with `risk_check_passed = false` and the machine-readable reason code shown
in parentheses, so every consumed card leaves a durable audit record (drops are deliberate
outcomes, **not** failures):
- `direction = hold` (`direction_hold`);
- `now > expires_at` (`card_expired`);
- `confidence < MIN_CONFIDENCE` (`below_min_confidence`);
- (`ticker`, `exchange_code`) is not an active watchlist instrument (`not_in_watchlist`);
- a live position or working order already exists for the instrument
  (**one live position per instrument**) (`position_exists`);
- the `thesis_card_id` has already been acted upon (`duplicate_card`) — idempotency is enforced by
  a UNIQUE constraint on `t_trade_decisions.thesis_card_id`, so a redelivery race cannot create a
  second decision for the same card;
- the shared review for `thesis_card_id` is missing or not `approved` (`review_not_approved`);
- `time_horizon` has no entry in `TIME_HORIZON_DAYS_MAP` (`horizon_unmapped`) — an exit window must
  be derivable before entry.

`direction = sell` cards are admitted and open **short** positions, symmetric with buys; all
notional caps apply to absolute exposure. Short-specific constraints (borrow availability, margin)
are delegated to IBKR at submission; a rejection for unavailable borrow is recorded like any other
broker rejection.

### 3.3 Price discovery

TradeExecutor refreshes a quote for the instrument directly from IBKR (snapshot market data). If the
quote is missing or older than the configured freshness bound, the pipeline **fails closed**: a
decision row is recorded with `risk_check_passed = false` and reason `quote_unavailable`, and no
order is placed. It then loads the MarketData context snapshot for `atr_20d` and support/resistance
levels; a missing or non-positive `atr_20d` is also a fail-closed reason (`atr_unavailable`).

### 3.4 Level construction (ATR bracket + R-multiple)

Let `entry` be the reference execution price (Section 3.6), `k = ATR_STOP_MULT`, and
`R = TAKE_PROFIT_R`:

- `buy`: `stop = entry − k · atr_20d`; `target = entry + R · (entry − stop)`.
- `sell`: `stop = entry + k · atr_20d`; `target = entry − R · (stop − entry)`.

The ATR-scaled stop adapts protective distance to each instrument's realized volatility; the
take-profit is set at a fixed reward-to-risk multiple of that distance.

### 3.5 Position sizing (risk-based off the card)

Position size honors the card's stated maximum loss:

```
qty = floor(risk_box.max_loss_usd / |entry − stop|)
```

Because the stop distance equals `k · atr_20d`, a wider (more volatile) stop yields a smaller
position, holding per-trade dollar risk near `max_loss_usd`. The quantity is then clamped so notional
does not exceed `MAX_POSITION_SIZE` or the remaining portfolio headroom (Section 3.6). If the
resulting `qty < 1`, the card is rejected (reason `size_below_one_share`).

### 3.6 Risk gate (portfolio guardrails)

Before submission, the following are evaluated atomically against current portfolio state.
**Working entry orders count as exposure:** the open-position count and `deployed_capital` include
both filled open positions and the reserved notional of submitted-but-unfilled entry orders, so a
batch of concurrently admitted cards cannot collectively breach a cap while earlier entries are
still filling. TradeExecutor runs as a **single instance**; the guardrail arithmetic assumes
exactly one consumer mutates portfolio state.

- open positions + working entries `< MAX_POSITIONS`;
- `deployed_capital + new_notional ≤ MAX_PORTFOLIO_EXPOSURE`;
- per-instrument exposure cap (satisfied by the one-position-per-instrument rule) and a best-effort
  per-sector cap (`MAX_SECTOR_EXPOSURE`); sector is derived best-effort from
  `shared.t_instruments.identifiers`, and the sector cap is skipped when sector is unknown;
- **daily-loss kill-switch:** if realized + unrealized PnL for the trading day
  `≤ −DAILY_LOSS_LIMIT`, all new entries are halted. Realized PnL comes from `t_daily_risk`;
  unrealized PnL is marked from the live IBKR session's portfolio updates at gate-evaluation time.
  The halt **latches**: once tripped, `t_daily_risk.halted` remains set for the remainder of the
  trading day even if PnL subsequently recovers. Open positions continue to be managed;
- `MAX_DAILY_TRADES` for the trading day is not exceeded.

The trading day is the calendar date in `TRADING_DAY_TIMEZONE` (default `America/New_York`); daily
counters and the kill-switch reset at midnight in that zone.

A `t_trade_decisions` row is persisted with `risk_check_passed` and `risk_check_details` **whether
the gate passes or fails**, so every card leaves an audit record.

### 3.7 Order placement (smart limit execution)

For an admitted, risk-approved decision, TradeExecutor submits an IBKR **bracket** as a single OCA
group:
- **parent** — a marketable limit (`LMT`) entry priced at `ask + slippage_buffer` (buy) or
  `bid − slippage_buffer` (sell), routed via `SMART`. Naked market orders are avoided.
- **child stop** (`STP`) at `stop`.
- **child take-profit** (`LMT`) at `target`.

The stop and take-profit children share an OCA group so a fill on one cancels the other.
`outsideRth` follows configuration. A `t_trade_executions` row is written per leg with status
`submitted`. If the parent remains unfilled after `ORDER_FILL_TIMEOUT_SECONDS`, it is cancelled and
re-priced once from a fresh quote; if still unfilled, the attempt is abandoned and the decision is
closed out. The stop and take-profit remain anchored to the original reference entry price across
re-pricing and fills — the resulting per-share risk drift (bounded by `ENTRY_LIMIT_SLIPPAGE_BPS`
plus one re-price step) is accepted rather than re-deriving levels from the actual average fill.

**Partial fills:** if the entry is partially filled when the timeout elapses, the unfilled
remainder is cancelled, the stop and take-profit children are resized to the filled quantity, and
the position is recorded at the partial size (entry leg status `partial`). Risk only shrinks in
this case: dollar risk scales down with quantity while the stop distance is unchanged.

### 3.8 Lifecycle management

TradeExecutor subscribes to IBKR `orderStatus`, `execDetails`, and `commissionReport` callbacks and
maintains position state (average entry price, filled quantity, commission, realized PnL). Exit
handling:
- **Bracket exit:** a stop or take-profit fill closes the position and cancels the sibling leg.
- **Time exit:** `time_horizon` maps to a maximum holding window (`TIME_HORIZON_DAYS_MAP`). When the
  window elapses, TradeExecutor cancels any residual bracket legs and flattens the position with a
  marketable limit order, tagging the exit reason `time`. If the window elapses outside regular
  trading hours and `OUTSIDE_RTH` is false, the flatten is placed at the next regular-session open.

On startup, TradeExecutor **reconciles** against IBKR (open orders and current positions) and its own
tables so a restart neither double-trades an in-flight card nor orphans an open bracket.
Reconciliation also covers the crash window between persisting a decision and submitting its order:
a decision with `risk_check_passed = true` but no execution rows and no matching broker order is
closed out with reason code `decision_orphaned`. Orphaned passed decisions are **never
auto-resubmitted** — a restart must not initiate trades (fail closed); the lost signal is visible in
the audit trail.

## 4. Failure Handling

- Missing or stale execution quote, missing ATR, sizing below one share, or a broker rejection are
  recorded as rejected/failed decisions with concise machine-readable reason codes; the source
  message is acknowledged (these are deterministic outcomes, not retryable).
- Transient infrastructure errors (database, Redis, IBKR RPC) leave the message unacknowledged for
  redelivery; after `max_delivery_attempts` it is dead-lettered.
- On IBKR disconnect, TradeExecutor pauses new entries, continues reconciling and managing existing
  positions where possible, and reconnects with backoff.
- TradeExecutor publishes nothing downstream; it is a terminal sink.

## 5. Execution Environment and Safety

- **Trading mode:** `TRADE_EXECUTOR_TRADING_MODE` defaults to `paper`. Mode and port must **agree in
  both directions**: the component refuses to start when the mode is `paper` but `IBKR_PORT` is a
  live-account port (the port, not the flag, determines which account receives orders — this
  misconfiguration would silently trade a live account), and likewise when the mode is `live` but
  the port is a paper port. Live trading therefore requires both the explicit `live` override and a
  live-account port.
- **IBKR ports:** `7497` (TWS paper) / `4002` (Gateway paper) by default; `7496` / `4001` for live.
- **Client id:** TradeExecutor uses a dedicated `IBKR_TRADE_EXECUTOR_CLIENT_ID`, distinct from
  MarketData's client id, so both may connect concurrently.
- **Kill-switch:** the daily-loss halt (Section 3.6) is the primary automated circuit-breaker; it
  blocks new entries for the remainder of the trading day once tripped.

## 6. Notes

- Runtime persistence is defined in `trade-executor/data-model.md`; executable DDL lives in
  `src/product_components/trade_executor/db/schema.sql`.
- Configuration knobs are enumerated in `trade-executor/configuration.md`.
- Consumer-group, settings-layering, schema-bootstrap, and deployment patterns follow the existing
  ThesisBuilder component.
