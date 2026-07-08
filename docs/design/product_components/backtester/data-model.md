# Backtester Data Model

Tables owned by the Backtester component.
PostgreSQL schema: `backtester`.

## Logical Model

### `t_backtest_runs`

Purpose:
- Durable metadata and summary for one on-demand historical backtest run.

Logical fields:
- `run_id` (primary key): stable run identity created at trigger time.
- `window_start_at`: simulated window lower bound (UTC).
- `window_end_at`: simulated window upper bound (UTC).
- `mode`: `replay` or `regeneration`.
- `timing_scenario`: entry timing simulated (`ideal`, `actual`, or `both`).
- `ideal_fetch_delay_seconds`, `ideal_thesis_delay_seconds`: feasible pipeline delays used to compute
  `t_ideal`.
- `dataset_snapshot_hash`: deterministic hash of the selected card-set and bar-set membership.
- `card_population`: card scope simulated (`all`, `approved_only`, or `rejected_only`).
- `strategies_requested`: optional strategy subset requested for the run.
- `initial_capital`: starting simulated capital.
- `execution_model_snapshot_json`: immutable execution mode plus slippage/commission/order-validity
  and live-parity or legacy exit parameters used.
- `risk_model_snapshot_json`: immutable sizing/exposure/max-position/cooldown/loss-limit parameters
  used.
- `thesis_config_snapshot_json`: regeneration-mode ThesisBuilder override config; null in replay mode.
- `run_note`: optional operator note from trigger payload.
- `status`: `running`, `completed`, or `failed`.
- `error_code`: nullable machine-readable terminal error.
- `error_details_json`: nullable structured terminal error context.
- `cards_considered`: thesis cards in the selected window before population filtering.
- `cards_in_population`: cards retained after applying `card_population`; expiry relative to "now" is
  never a filter.
- `cards_live_executable`: subset that was `approved` and unexpired at decision time (the live-fidelity
  slice); reported for comparison, not used to gate simulation.
- `cards_skipped_no_price`: in-population cards skipped for missing historical bars.
- `trades_opened`: simulated positions opened.
- `trades_closed`: simulated positions reaching a terminal closed exit.
- `trades_risk_blocked`: candidates blocked by a binding portfolio/risk rule.
- `net_pnl`, `gross_profit`, `gross_loss`, `total_commission`, `total_slippage`: P&L decomposition.
- `total_return`: net return on initial capital.
- `win_rate`, `avg_win`, `avg_loss`, `profit_factor`, `expectancy`: trade-quality metrics.
- `max_drawdown`, `max_drawdown_duration_seconds`: drawdown metrics from the equity curve.
- `sharpe_ratio`: configured risk-free-rate Sharpe.
- `exposure_fraction`: time-in-market fraction.
- `signal_accuracy`: share of trades whose realized direction matched card direction.
- `avg_news_fetch_delay_seconds`, `p95_news_fetch_delay_seconds`, `max_news_fetch_delay_seconds`:
  NewsFetcher delay aggregates over the population.
- `avg_thesis_build_delay_seconds`, `p95_thesis_build_delay_seconds`, `max_thesis_build_delay_seconds`:
  ThesisBuilder delay aggregates.
- `avg_total_pipeline_delay_seconds`, `p95_total_pipeline_delay_seconds`,
  `max_total_pipeline_delay_seconds`: end-to-end delay aggregates.
- `pnl_gap`, `win_rate_gap`, `trades_flipped_by_delay`: ideal-vs-actual gap metrics; populated only
  when `timing_scenario = both`, otherwise null.
- `llm_token_budget_limit`: regeneration-mode per-run token ceiling; null in replay mode.
- `summary_json`: structured run-level and per-strategy metric aggregates.
- `summary_md`: human-readable run summary for operators.
- `created_at`, `started_at`, `finished_at`: lifecycle timestamps.

Behavioral constraints:
- One immutable row per `run_id`; status transitions are monotonic and audit-safe.
- Count fields must be non-negative; ratio metrics are stored only when their denominator is defined,
  following `docs/design/product_components/backtester/behavior.md` Section 7.1.
- `thesis_config_snapshot_json` and `llm_token_budget_limit` are required when `mode = regeneration`
  and null when `mode = replay`.
- `ideal_fetch_delay_seconds` and `ideal_thesis_delay_seconds` must be non-negative.
- Gap metrics (`pnl_gap`, `win_rate_gap`, `trades_flipped_by_delay`) are non-null only when
  `timing_scenario = both`.

### `t_llm_analysis_cache`

Purpose:
- Durable cache of deterministic ThesisBuilder article-analysis LLM responses used only by
  Backtester regeneration runs.

Logical fields:
- `llm_model`, `max_output_tokens`, `prompt_sha256` (composite primary key): cache identity for
  the exact model, output-token cap, and canonical prompt string.
- `article_id`, `ticker`, `exchange_code`: supporting debug/invalidation columns; not part of
  cache identity.
- `response_json`: raw structured response JSON returned by the LLM client after API usage token
  normalization and before ThesisBuilder semantic validation.
- `created_at`, `last_used_at`: cache lifecycle timestamps.

Behavioral constraints:
- Stored in the durable `backtester` schema, not the per-run `sim_bt_*` schemas, so cache entries
  survive repeated runs and sim-schema teardown.
- Used only by regeneration wiring. Live ThesisBuilder analysis remains uncached.
- Cache hits are reported as budget-free by the regeneration analyzer; real LLM token usage remains
  attributable to cache misses.

### `t_backtest_trades`

Purpose:
- Per-simulated-trade record for one card candidate within a run.

Logical fields:
- `trade_id` (primary key): stable trade identity.
- `run_id`: parent run identity.
- `thesis_card_id`: copied card id from the ThesisBuilder export (lineage only, not a cross-schema FK).
- `ticker`, `exchange_code`: canonical instrument identity.
- `strategy`: card strategy used for per-strategy breakdown.
- `direction`: `buy` or `sell`.
- `card_decision_state`: copied live decision state of the source card (`approved` or `rejected`).
- `card_was_live_expired`: whether the card was past its `expires_at` at `t_entry` under the live
  freshness gate; recorded for slicing only and does not affect simulated entry/exit.
- `entry_timing_scenario`: `ideal` or `actual`; identifies which timing produced this trade row.
- `news_published_at`: triggering evidence article publication time (`news_ready_at`).
- `news_fetched_at`: triggering evidence article ingestion time.
- `card_created_at`: real production card creation time (`t_actual`).
- `news_fetch_delay_seconds`: `news_fetched_at − news_published_at`.
- `thesis_build_delay_seconds`: `card_created_at − news_fetched_at`.
- `total_pipeline_delay_seconds`: `card_created_at − news_published_at`.
- `entry_at`, `entry_price`, `quantity`: entry fill (entry time is `t_ideal` or `t_actual` per
  `entry_timing_scenario`).
- `exit_at`, `exit_price`: exit fill (null when not filled or risk blocked).
- `gross_pnl`, `commission`, `slippage`, `net_pnl`, `return_pct`: trade economics.
- `exit_reason`: `take_profit`, `stop_loss`, `time_stop`, `reversal`, `window_end`, `not_filled`, or
  `risk_blocked`.
- `risk_block_rule`: nullable binding rule name when `exit_reason = risk_blocked`.
- `holding_period_seconds`: nullable realized holding period for closed trades.
- Excursion diagnostics (computed during the simulation bar walk; null for `not_filled` and
  `risk_blocked` trades; consumed by the backtest verification workflow,
  `docs/design/backtest-verification-methodology.md`):
  - `mfe_pct`, `mae_pct`: maximum favorable/adverse excursion over the holding period, signed by
    direction, at ±1-bar resolution.
  - `time_to_mfe_seconds`, `time_to_mae_seconds`.
  - `horizon_returns_json`: signed gross returns at the configured post-entry horizons
    (default 30/60/120/240 trading minutes plus 1/3/5 trading days, matching the card
    `time_horizon` scale), cost-free and exit-free; a horizon is null when the window ends
    before it.
  - `both_brackets_in_one_bar`: whether any bar in the trade spanned both the stop and the target
    (the trade's outcome depends on the intrabar tie-break assumption).
  - `bar_coverage_ratio`: bars present / bars expected over the holding period.
- `created_at`: row creation timestamp.

Behavioral constraints:
- `run_id` must reference an existing run.
- `thesis_card_id`, `ticker`, and `exchange_code` are copied identities and must not be enforced with
  cross-schema foreign keys.
- A trade counts as closed only when `exit_reason` is terminal and not in
  (`not_filled`, `risk_blocked`).
- Delay fields are copied from the ThesisBuilder export and are independent of `entry_timing_scenario`
  (the same card carries identical delays in both its `ideal` and `actual` rows).
- At most one trade row per `(run_id, thesis_card_id, entry_timing_scenario)`, so a `both` run stores
  one `ideal` and one `actual` row per card.

### `t_backtest_equity_points`

Purpose:
- Time-ordered equity-curve samples for drawdown computation and charting.

Logical fields:
- `point_id` (primary key): stable point identity.
- `run_id`: parent run identity.
- `timing_scenario`: `ideal` or `actual`; identifies which equity curve the point belongs to.
- `as_of`: sample timestamp (UTC).
- `equity`: simulated total equity (cash plus mark-to-market open positions).
- `open_positions`: number of open simulated positions at the sample.
- `created_at`: row creation timestamp.

Behavioral constraints:
- `run_id` must reference an existing run.
- Points are append-only and ordered by `as_of` within a `(run_id, timing_scenario)` curve.
- A `both` run produces two curves; `max_drawdown` on the run row is from the `ideal` curve, and the
  gap metrics compare the `ideal` and `actual` curves.

### `t_backtest_card_snapshots`

Purpose:
- Audit-stable copy of the decision-time thesis card inputs used by the run, so results remain
  reproducible even after ThesisBuilder evolves its private tables.

Logical fields:
- `snapshot_id` (primary key): stable snapshot identity.
- `run_id`: parent run identity.
- `thesis_card_id`: copied card id from the ThesisBuilder export.
- `ticker`, `exchange_code`: canonical instrument identity.
- `direction`, `strategy`, `time_horizon`, `confidence`: copied card fields.
- `decision_state`: copied card decision state at export time.
- `card_created_at`, `card_expires_at`: copied card lifecycle timestamps.
- `evidence_json`: copied evidence bullets with article references and, per article, both
  `published_at` and `fetched_at`, so pipeline delays stay reproducible after ThesisBuilder evolves.
- `news_ready_at`: `max(evidence.published_at)`, the timing basis for `t_ideal`.
- `risk_box_json`: copied risk box (max loss, stop condition, invalidation condition).
- `source_export_ref`: identifier of the ThesisBuilder export/batch the card was copied from.
- `created_at`: row creation timestamp.

Behavioral constraints:
- `run_id` must reference an existing run.
- All copied ids are lineage fields and must not be enforced with cross-schema foreign keys; the
  ThesisBuilder-owned tables remain private to ThesisBuilder.
- At most one snapshot per `(run_id, thesis_card_id)`.

## Write Semantics

- Insert the run row first with `status = 'running'`.
- Insert card snapshots for the in-population card set before simulation begins.
- Insert trade rows and equity points incrementally as the simulated clock advances.
- Update run aggregate metrics in place during execution.
- Finalize the run with terminal `status` and `finished_at`; no further trade or equity writes after
  the terminal transition.

## External Data Dependencies (Not Owned)

The component consumes but does not own:
- ThesisBuilder thesis cards, evidence, and decision state — supplied through a ThesisBuilder-owned
  card-history export API/contract.
- MarketData historical OHLCV bars — supplied through the MarketData historical-bars read API.

The Backtester must not query ThesisBuilder-owned or MarketData-owned tables directly, must not call
external market-data providers directly, and must copy any data it needs for audit into its own
schema.

## Notes

- Executable PostgreSQL DDL should be added under `src/product_components/backtester/db/schema.sql`
  during implementation, with physical column types, check constraints (status/mode/exit-reason
  enumerations including `timing_scenario`/`entry_timing_scenario`, non-negative counts and delays,
  ratio bounds, terminal-completion consistency), and indexes (run listing by
  `(status, created_at DESC, run_id)`; trades by
  `(run_id, entry_timing_scenario, strategy, thesis_card_id)`; equity points by
  `(run_id, timing_scenario, as_of)`).
- Design docs define logical ownership and behavior constraints; runtime DDL must come from
  source-managed SQL or migrations.
