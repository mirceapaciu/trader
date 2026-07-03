# Backtester Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the Backtester process.

The Backtester is the Phase 7 component that validates trading strategies historically before
capital is risked. It replays the decision-to-execution stage of the pipeline over a historical
window using point-in-time data, simulates order fills and exits against historical price bars,
applies the same risk and position-sizing rules as live trading, and reports per-strategy
performance metrics.

Main goal: show whether profitable trades are possible with the thesis cards this system produces.
The Backtester answers two questions:

1. **What trades are possible under ideal conditions?** Simulate entry at the ideal time the system
   could have created the card if it were always on (news publication plus a feasible pipeline
   delay). This isolates thesis quality from operational latency.
2. **How far are we from ideal?** Capture the actual per-stage delays (NewsFetcher fetch delay and
   ThesisBuilder processing delay) for each card and quantify, in P&L terms, what those delays cost
   by also simulating at the actual card-creation time and reporting the gap.

Backtester responsibilities:
- Run on-demand historical simulations over a requested time window for one or more strategies.
- Source trade candidates exclusively from validated thesis cards (honoring the product
  constraint: No Thesis Card, No Trade).
- Reconstruct decision-time state without look-ahead bias.
- Simulate entry fills, exit rules, commissions, and slippage with a deterministic execution model.
- Apply portfolio-level risk, sizing, and cooldown rules to construct a simulated equity curve.
- Compute and persist run-level and per-trade performance metrics for auditability and comparison.

Out of scope:
- Live or paper order placement of any kind (the Backtester never connects to a broker).
- Continuous ingestion of live provider feeds or queue consumption.
- News fetching, article attribution, or filter-quality evaluation (owned by NewsFetcher and the
  Filter Quality Evaluator).
- Producing executable signals on `signal_queue` or writing to live trading tables.

The Backtester is a sibling offline analytic tool to the
`docs/design/product_components/filter-quality-evaluator/behavior.md` evaluator: both are
operator-driven, idempotent-per-run, and read immutable historical snapshots without affecting the
production pipeline.

## 2. Process Contract

Process name: backtester

Inputs:
- Historical thesis cards from the ThesisBuilder card-history export API/contract (see
  Section 9 and `docs/design/product_components/backtester/data-model.md`).
- Historical OHLCV bars from the MarketData historical-bars read API (see Section 9).
- Run parameters (window, mode, strategy set, execution model, risk model, initial capital).
- LLM provider credentials and policy (regeneration mode only).

Outputs:
- One run summary row in `backtester.t_backtest_runs`.
- Per-simulated-trade rows in `backtester.t_backtest_trades`.
- Equity-curve points in `backtester.t_backtest_equity_points`.
- Copied decision-time card snapshots in `backtester.t_backtest_card_snapshots` for audit stability.
- A human-readable run summary in `backtester.t_backtest_runs.summary_md`.

Delivery semantics:
- On-demand execution only; the process is not a continuously running consumer.
- Idempotent run creation based on an explicit `run_id` generated at trigger time.
- A run evaluates one immutable data snapshot defined by its parameters; retries create a new
  `run_id` and never mutate completed results.

## 3. Backtest Modes and Timing Scenarios

The Backtester supports two modes selected per run.

### 3.1 Replay mode (default)

- Trade candidates are the thesis cards ThesisBuilder already produced during live or paper running
  within the window.
- No LLM calls are made; the run is fully deterministic and cheap.
- Used to tune the execution and exit model, position sizing, and portfolio construction without
  re-deriving cards.

Card population (default: all cards):
- Verifying thesis cards is the whole point of a backtest, and almost every historical card has
  already passed its live `expires_at`. Expiry relative to wall-clock "now" must therefore never be
  an eligibility filter — doing so would exclude essentially the entire historical corpus. A card's
  `expires_at` is a live-execution freshness gate and is interpreted only against the simulated clock
  (Section 4), never against the current time.
- By default the Backtester simulates a position for every card in the window so each thesis can be
  verified on its own merits, regardless of live `decision_state` or whether the card has since
  expired. This includes:
  - `approved` cards (what live/paper trading would have executed),
  - `rejected` cards, including `stale_evidence` cards that ThesisBuilder generated only to estimate
    missed opportunities from old news.
- Every simulated trade records the source card's live `decision_state` and a `card_was_live_expired`
  flag, so run metrics can be reported both over the full population and restricted to the
  live-executable subset (approved and unexpired at decision time). This lets an operator compare
  "what we actually traded live" against "what the thesis would have yielded if always executed."
- The `card_population` run parameter narrows this when desired: `all` (default), `approved_only`
  (live-fidelity), or `rejected_only` (audit missed opportunities).

### 3.2 Regeneration mode (optional, expensive)

- Re-runs ThesisBuilder analysis over the historical news of the window using an alternate
  configuration snapshot (prompt, thresholds, strategy set), then backtests the resulting cards.
- Used to evaluate prospective ThesisBuilder/strategy changes, not just execution parameters.
- Subject to an explicit per-run LLM token budget; fails closed when the budget is exhausted before
  completion.
- Carries the documented backtesting caveats: regenerated LLM analysis can differ from the original
  real-time analysis because point-in-time context is only approximately reconstructable.
- Regeneration must request market context reconstructed as-of each card's decision time and must not
  use any data published after that time.

### 3.3 Timing scenarios

A timing scenario decides when a simulated position is entered. It is orthogonal to mode and selected
per run via `timing_scenario` ∈ {`ideal` (default), `actual`, `both`}.

Per card, three timestamps are defined:

- `news_ready_at` = `max(evidence.published_at)`. The card cannot form before its last evidence
  article is published, so this is the earliest moment the thesis could exist.
- `t_ideal` = `news_ready_at + ideal_fetch_delay + ideal_thesis_delay`. The ideal entry time assuming
  an always-on system at feasible latency (defaults: 120 s fetch + 60 s thesis). Example: news at
  10:00 → fetched at 10:02 → card at 10:03 → entry at 10:03.
- `t_actual` = card `created_at`. The real production creation time, reflecting the latency actually
  incurred.

Scenario behavior:

- `ideal` (primary): enter at `t_ideal`. Answers "what trades are possible under ideal conditions?"
- `actual`: enter at `t_actual`. Reflects the latency the system actually had.
- `both`: simulate the card under both `ideal` and `t_actual`, producing two trade rows
  (distinguished by `entry_timing_scenario`). The run then reports the per-card and aggregate P&L gap
  (`ideal_pnl − actual_pnl`) as the cost of latency. Answers "how far are we from ideal?"

The no-look-ahead rules in Section 4 apply to whichever entry time is active for the scenario.

### 3.4 Pipeline delay capture

For every simulated trade the Backtester records the actual delays observed in production, so reports
can break the results down by delay and trace where latency is introduced:

- `news_fetch_delay = fetched_at − published_at` (NewsFetcher delay; measured on the triggering
  evidence article, i.e. the one defining `news_ready_at`).
- `thesis_build_delay = card_created_at − fetched_at` (ThesisBuilder delay).
- `total_pipeline_delay = card_created_at − published_at`.

These are computable from the ThesisBuilder card-history export alone: each evidence article carries
`published_at` and `fetched_at` (copied from `t_news_analyses.article_snapshot`) and the card carries
`created_at`. The delays are stored on every trade row regardless of timing scenario and are also
copied into `t_backtest_card_snapshots` for reproducibility.

### 3.5 Latency measurement ownership (where "how far from ideal" belongs)

The "how far from ideal" question has two parts that belong in different places:

- The **continuous latency/health** measurement — current fetch and thesis delays, trends, and
  alerting — is operational observability. It does not need historical price replay and is better
  owned by the live pipeline and the Monitoring UI, which already exposes `end_to_end_latency_seconds`
  and ThesisBuilder pending-age KPIs. Recommended follow-up (out of scope here): add per-stage
  fetch-delay and thesis-delay tiles to the Monitoring UI ThesisBuilder tab.
- The **trade-outcome cost of latency** — how many dollars and which wins/losses the delay changed —
  genuinely requires mapping a delay to a price outcome, which only the Backtester can do. The
  Backtester therefore owns this part through the `both` timing scenario and the ideal-vs-actual gap
  metrics, and records the raw delays for its window so results can be sliced by delay.

## 4. Point-in-Time Correctness (No Look-Ahead)

Look-ahead bias invalidates a backtest, so the following are hard rules.

- The entry time `t_entry` is the active timing scenario's time (`t_ideal` or `t_actual`, Section
  3.3). Position entry may only use bars at or after `t_entry`.
- A card's `expires_at` is evaluated only against the simulated clock, not wall-clock "now": a card
  that has expired relative to the present is still entered at its historical `t_entry`. Live expiry
  does not drive simulated exits; holding period is governed by the strategy time stop (Section 5.2).
  The `card_was_live_expired` flag records, for slicing only, whether the card would have been past
  its `expires_at` at `t_entry` under the live freshness gate.
- Exit evaluation may only consume bars strictly after the entry fill bar.
- In regeneration mode, any market context used for validation or sizing must be derived only from
  bars with `bar_start_at <= t_entry`.
- Historical bars must be requested with explicit `[start, end]` bounds and the simulator must never
  read a bar whose interval has not closed by the simulated clock.
- Survivorship bias is recorded as a known limitation: only instruments with available historical
  bars are simulated, and delisted instruments may be absent. The run summary reports the count of
  cards skipped for missing price history.

## 5. Execution Simulation Model

The simulator advances a per-instrument chronological clock over historical bars and applies a
deterministic fill model. The model is configurable but defaults to conservative assumptions.

### 5.1 Entry

- `market` order: fills at the open of the first bar at or after `t_entry`, adjusted by configured
  entry slippage in basis points (adverse direction).
- `limit` order: fills only if the bar range crosses the limit price within the configured order
  validity window; otherwise the candidate expires unfilled and is recorded with
  `exit_reason = not_filled`.
- Quantity comes from the position-sizing rules in Section 6.

### 5.2 Exit

Exit rules are evaluated per bar in chronological order, reusing the strategy rules from
`docs/trading-strategies.md`:

- take-profit target,
- stop-loss,
- time stop (max holding period),
- reversal exit (a later opposing approved card for the same instrument).

First-touch wins. When a single bar's range touches both the stop and the target (intrabar
ambiguity), the simulator resolves conservatively and assumes the stop filled first. Exit fills
apply configured exit slippage in the adverse direction. If no rule triggers before the window ends,
the position is closed at the last available bar with `exit_reason = window_end`.

### 5.3 Costs

- Commission is applied per fill using a configurable IBKR-like model (per-share with a minimum, or
  flat per trade).
- Slippage is applied to both entry and exit fills.
- Both are deducted from realized P&L and reported separately for transparency.

## 6. Risk, Sizing, and Portfolio Construction

The Backtester applies the same logical rules as the live risk manager so results are representative:

- Fixed-fractional position sizing keyed on card confidence/signal strength.
- Per-ticker max position size and per-position portfolio share cap.
- Max total portfolio exposure and max positions per sector.
- Daily trade cap and per-ticker cooldown.
- Daily loss circuit breaker: once tripped on a simulated day, no new positions open that day.

When a card cannot be opened because a portfolio constraint is binding, the trade is recorded with
`exit_reason = risk_blocked` and the binding rule is captured for attribution. Sizing and risk
parameters are run inputs (Section 7 of the configuration spec) so a single historical card set can
be evaluated under different risk regimes.

## 7. Metrics

Run-level metrics (persisted on `t_backtest_runs.summary_json` and scalar columns):

- total return, net P&L, gross P&L, total commission, total slippage,
- number of trades, win rate, average win, average loss, win/loss ratio,
- profit factor (gross profit / gross loss), expectancy per trade,
- max drawdown (peak-to-trough on the equity curve), max-drawdown duration,
- Sharpe ratio (configurable risk-free rate and periodicity),
- exposure (time-in-market fraction),
- signal accuracy (share of trades whose realized direction matched card direction),
- counts of cards skipped for missing price history and trades that were `risk_blocked`.

Per-strategy breakdown of the same trade-level metrics is produced for each `strategy` present in the
card set, enabling the strategy-tuning workflow in `docs/trading-strategies.md` Section 7.

Additional required breakdowns:

- By card status: `approved`, `rejected`, and `stale_evidence`, plus the `card_was_live_expired`
  slice, so the live-executable subset can be compared with the full thesis population.
- By pipeline-delay bucket (configurable bucket edges in seconds), reported for `news_fetch_delay`,
  `thesis_build_delay`, and `total_pipeline_delay`, with avg/p95/max of each delay on the run row.

Ideal-vs-actual gap metrics (populated only when `timing_scenario = both`):

- `pnl_gap` = ideal net P&L − actual net P&L (the dollar cost of latency).
- `win_rate_gap` = ideal win rate − actual win rate.
- `trades_flipped_by_delay` = count of cards whose outcome changed from win to loss (or filled to
  not-filled) between the ideal and actual timing.

### 7.1 Normative Metric Rules

- `win_rate = winning_trades / closed_trades`; if `closed_trades = 0`, store `NULL`.
- `profit_factor = gross_profit / gross_loss`; if `gross_loss = 0` and `gross_profit > 0`, store
  `NULL` and flag `profit_factor_undefined = true` in `summary_json`; if both are 0, store `NULL`.
- `expectancy = net_pnl / closed_trades`; if `closed_trades = 0`, store `NULL`.
- `max_drawdown` is computed on the realized equity curve in `t_backtest_equity_points`, expressed as
  a non-negative fraction of the running peak equity.
- A trade counts as closed only when it reached a terminal `exit_reason` other than `not_filled` or
  `risk_blocked`.
- Scalar ratio metrics are stored rounded to 5 decimal places (half-up).

## 8. Triggering and Run Lifecycle

Triggering:
- Direct CLI execution of `backtester` (e.g. `uv run python -m src.product_components.backtester`).
- Monitoring UI trigger: the Backtest tab starts a bounded run via `POST /api/backtests`, which runs
  as an in-process background run inside the Monitoring UI backend (see
  `docs/design/product_components/monitoring-ui/behavior.md` Section 4.6). The UI path targets bounded
  `replay` runs; long or expensive `regeneration` runs should be started from the CLI.

Trigger payload:
- `window_start_at` (UTC), `window_end_at` (UTC), with `window_start_at < window_end_at`.
- `mode` (`replay` default, or `regeneration`).
- `timing_scenario` (`ideal` default, `actual`, or `both`).
- `ideal_fetch_delay_seconds`, `ideal_thesis_delay_seconds` (override the configured ideal pipeline
  delay used to compute `t_ideal`).
- `card_population` (`all` default, `approved_only`, or `rejected_only`).
- `strategies` (optional subset; default all strategies present in the card set).
- `initial_capital`.
- `execution_model_snapshot_json` (slippage, commission, order validity) — defaults applied when omitted.
- `risk_model_snapshot_json` (sizing, exposure caps, cooldown, loss limit) — defaults applied when omitted.
- `thesis_config_snapshot_json` (regeneration mode only; the ThesisBuilder override config to replay).
- `run_note` (optional).

Lifecycle states:
1. `running`
2. `completed`
3. `failed`

Run policy:
- A direct CLI invocation creates exactly one run request and one `run_id`.
- The selected card set and bar set form an immutable snapshot; `dataset_snapshot_hash` is the
  deterministic hash of that membership.
- Retries create a new `run_id`; completed results are never mutated.
- Partial per-instrument simulation failures are recorded per trade and do not invalidate completed
  trades; hard failures (e.g. unreadable inputs, exhausted LLM budget in regeneration mode) finalize
  the run as `failed` with a machine-readable `error_code`.

## 9. Integration Boundaries

The Backtester honors the component database encapsulation rules in
`docs/design/overview.md` Section 3.3 and `docs/design/data-model.md`. It reads only through owning
component contracts and copies the data it needs into its own schema for audit stability.

Producer components and required read contracts:
- ThesisBuilder must expose a card-history export API/contract that returns validated thesis cards
  for a time window with their evidence, strategy, direction, confidence, risk box, decision state,
  `created_at`, and `expires_at`. Each evidence article must carry `published_at` and `fetched_at`
  (copied from `t_news_analyses.article_snapshot`) so the Backtester can compute pipeline delays
  (Section 3.4). The Backtester must not query `thesis_builder`-owned tables directly. This contract
  is the card analogue of the NewsFetcher evaluation dataset export consumed by the Filter Quality
  Evaluator.
- MarketData must expose a historical-bars read API,
  `get_historical_bars(ticker, exchange_code, interval, start, end)`, returning normalized OHLCV
  bars at the requested interval. The Backtester requires at least 1-minute resolution and relies on
  MarketData fetching missing ranges on demand and storing them durably so they are reused across
  runs (see `docs/design/product_components/market-data/behavior.md` Section 4). The Backtester must
  not query `market_data`-owned tables directly and must not call external market-data providers
  itself. During warmup the Backtester wires an IBKR market-data session into MarketData so bars are
  fetched from IBKR first when a Gateway/TWS is reachable, falling back to Polygon/Alpha Vantage when
  IBKR is unavailable (see MarketData behavior Section 2). The session is best-effort: if IBKR cannot
  be reached, warmup proceeds on the fallback providers.
- In regeneration mode the Backtester invokes ThesisBuilder analysis through a ThesisBuilder-owned
  replay entry point with an immutable, run-scoped config snapshot; it must not mutate global
  ThesisBuilder configuration or production tables.

No-coupling requirements:
- The Backtester never publishes to `news_raw_queue` or `signal_queue` and never writes to
  `thesis_builder`, `market_data`, `news_fetcher`, or `trade_executor` schemas.
- The live trading pipeline (ThesisBuilder, TradeExecutor) does not depend on Backtester runs.

Consumer components:
- Monitoring UI renders Backtester run summaries, equity curves, per-strategy and delay breakdowns,
  and per-trade results in its Backtest tab, and triggers bounded runs through its HTTP API. The UI
  reads `backtester` data only through read-only projections exposed by the Monitoring UI HTTP API.

## 10. Core vs Product Boundary

Per `docs/design/overview.md` Section 1.1 and Section 13, the deterministic, domain-agnostic parts of
this component are candidates for extraction into `src/core_components`: a point-in-time event-replay
harness (chronological clock, no-look-ahead guarantees, idempotent run snapshotting) and a generic
fill/equity-curve simulator are reusable beyond trading. Trading-specific concerns — strategy exit
rules, the thesis-card source contract, risk/sizing policy, and broker-like commission models —
remain in `src/product_components/backtester`.

## 11. Operational Constraints

- Expensive, operator-driven workload; not part of any polling or consumer loop.
- Secrets must be sourced from environment variables.
- Input and output data must remain within project data stores.
- Reliability: per-item failures are recorded and do not invalidate completed work; hard failures
  mark the run `failed` with a machine-readable error code.
