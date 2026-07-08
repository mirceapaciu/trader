# verify-backtest

Diagnose why a backtest run shows the P&L it shows. Verify the run is trustworthy, attribute the
P&L, measure suspected causes with single-factor counterfactual re-runs, judge thesis-card and
news-analysis quality ex-ante, and produce a verdict with ranked recommendations.

The canonical methodology — the 14-suspect catalog, verdict decision tree, judging discipline,
and default thresholds — is `docs/design/backtest-verification-methodology.md`. Read it before
starting. This file is the operational protocol.

**Stance: be critical of the configuration itself.** Every parameter in the generation and
trading chain — evidence thresholds, confidence gates, bracket percentages, time stop, card
expiry, risk-box values — is a hypothesis someone typed in, not a fact. Challenge each one for
internal coherence and provenance (methodology §2, "Configuration critique"). The canonical
example: cards claim `time_horizon = swing_1d_5d` while the execution model's 4 h time stop and
the card's 6 h expiry force every position closed intraday — the thesis is never tested at the
horizon it claims.

## Input

The user names a completed backtest `run_id` (from `backtester.t_backtest_runs`), or a time
window. If given only a window, first trigger a baseline replay run and wait for completion:

```python
# uv run python trigger_baseline.py
from datetime import datetime, timezone
from src.product_components.backtester.main import ...  # wire service per main.py
# Build BacktestRunParams(window_start_at=..., window_end_at=..., mode=REPLAY,
#   timing_scenario=BOTH, card_population=ALL, run_note="verify-backtest baseline")
# then BacktesterService.run(params)  — synchronous.
```

If the user asks a narrower question ("is it the costs?", "are the cards any good?"), run only
the stages that answer it, but always run Stage 1 first — never diagnose an untrustworthy run.

## Protocol

Connect to Postgres the same way as the fix-and-verify skill (load `.env.shared` → `.env.prod` →
`.env.secrets`, then psycopg). All reads outside the `backtester` schema must go through
documented contracts; this skill reads `backtester.*` tables (dev-tool exception, read-only) and
MarketData bars via `MarketDataService.get_historical_bars`. Never write to any component schema.

### Stage 1 — Integrity (always)

1. Load the run row: population, timing scenario, model snapshots, `cards_skipped_no_price`,
   counts. **For regeneration runs, check coverage before anything else:** a run can finish
   `status = 'completed'` with its LLM token budget exhausted mid-window (issue 260708-01).
   Read `summary_json -> 'regeneration' ->> 'budget_exhausted'`; if true, establish how far
   analysis actually got via `MAX((article_snapshot->>'published_at'))` in the run's sim schema
   (see Stage 3) or the `Regeneration token budget exhausted` line in
   `logs/monitoring-ui-backend.log`, and scope every downstream conclusion to the covered
   sub-window. Once 260708-01 is resolved, read the first-class columns
   (`budget_exhausted`, `analysis_coverage_until_at`, `llm_tokens_used`) instead.
2. SQL checks over `t_backtest_trades`: look-ahead (`entry_at >= news_ready_at` via card
   snapshots; `entry_at >= card_created_at` for actual-timing rows), share of trades from
   `card_decision_state = 'rejected'` and `card_was_live_expired = true`.
3. Excursion columns (`mfe_pct`, `mae_pct`, `horizon_returns_json`, `both_brackets_in_one_bar`,
   `bar_coverage_ratio`) are persisted by the Backtester on each closed trade. If the run
   predates those columns, re-run the baseline rather than reconstructing them ad hoc.
4. Config-parity audit: diff the run's `execution_model_snapshot_json` /
   `risk_model_snapshot_json` against the live rules in
   `src/product_components/trade_executor/pipeline.py` (`construct_levels`, `size_position`) and
   its settings. Also grep for generation outputs that no gate consumes (known instance:
   `price_impact_magnitude` is persisted but read by nothing).
5. Configuration critique (methodology §2): walk the parameter chain card-side
   (`thesis_builder` settings/defaults: evidence count/window, staleness, confidence gate,
   `time_horizon`, `expires_at`, risk box) and trade-side (time stop, brackets, sizing, cooldowns)
   and list every incoherent pair and every unvalidated default. Minimum checks: time_horizon vs
   time stop vs expiry (S14); target reachability within the time stop; risk-box fields nothing
   enforces; evidence cadence vs claimed horizon. Each item becomes a finding and, where a knob
   exists, a Stage 3 counterfactual.

Thresholds and the artifact rules are in the methodology doc §Stage 1 / verdict tree.

### Stage 2 — Attribution and edge

Run the committed script (do not improvise the statistics ad hoc):

```bash
uv run python scripts/verification/attribution.py --run-id <run_id> --scenario ideal
```

It emits (stdout JSON + a Markdown block): P&L slices by decision_state, strategy, direction,
confidence bucket, exit_reason, ticker, hour-of-day, delay decile; cost decomposition; loss
concentration; and the two headline statistics from persisted excursions — `edge_gross`
(bootstrap CI of mean signed 2h horizon return + binomial hit-rate test) and `edge_net`.
`edge_gross > 0 > edge_net` means cards work, trading doesn't. If the script does not exist yet,
write it there first (spec in methodology doc §Stage 2), commit it, then run it — never compute
these numbers inline in the session.

### Stage 3 — Counterfactual sweeps

**Generation-side factors first, analytically, over the sim schema.** Every regeneration run
leaves a surviving Postgres schema `sim_bt_<run_id>` holding the full generation funnel:
`t_news_analyses` (validation_status, rejection_reason_code, tokens_used, article_snapshot),
`t_evidence_windows` (collecting/expired/satisfied, article_ids), `t_thesis_cards`. Sweep
generation thresholds (evidence-collection window, required evidence count, relevance/confidence
gates) by re-simulating the grouping over the persisted valid analyses — zero LLM tokens, seconds
per cell — and only pay for an engine re-run to confirm the chosen cell. (Validated 2026-07-08:
the analytic sweep predicted 11 cards at a ~720-min window; the real 1000-min run produced 11.)
Trading-side factors still require engine re-runs:

One factor per re-run, via `BacktesterService.run` with `dataclasses.replace` on the baseline
params (reconstructed from the run row snapshots). v1 matrix (methodology doc §Stage 3):
`approved_only` population; zero costs; `intrabar_stop_before_target=False` (bounds pair);
take-profit/stop grid; time-stop grid **including day-scale holds (1 and 3 trading days) — the
entries that actually test a `swing_1d_5d` thesis at its stated horizon (S14); make sure the
run window extends far enough past the last entry for these to close**. Tag each
`run_note = "verify-backtest:<baseline>:<factor>"`.
Verify each child run's `dataset_snapshot_hash` matches the baseline (except population changes).
Report each factor's `pnl_delta` and the split-sample sign agreement (first vs second half of the
window) — a factor is confirmed only if the improvement sign agrees in both halves. The
min-confidence gate is estimated analytically over the baseline trade set (state the portfolio-
interaction caveat).

### Stage 4 — Card and analysis judging (you are the judge)

Sample per methodology §Stage 4: 15 worst losers + 10 best winners + 15 random closed
live-executable trades, joined to `t_backtest_card_snapshots`.

**Hard ordering rule (hindsight-bias control): write down all ex-ante judgments before looking at
any outcome data for the sampled cards.** Concretely: query cards + evidence + pre-entry context
only; produce and save the full ex-ante table; only then query outcomes.

- **Ex-ante, per card:** soundness score 0–100, direction agreement, failure modes from the fixed
  enum (methodology §Stage 4), one-line rationale.
- **Ex-ante, per analysis** (up to 3 source analyses per sampled card, from the ThesisBuilder
  export/evidence): the four-step magnitude audit — (1) concrete event, (2) transmission channel,
  (3) order-of-magnitude size vs company scale, (4) consistency of the generation model's
  direction/confidence/magnitude with that estimate. Record `quantification_present` and analysis
  failure modes (`unquantified_impact`, `channel_only_reasoning`, …).
- **Ex-post, per card:** with outcomes now loaded — outcome attribution
  (`thesis_wrong | thesis_right_execution_wrong | market_noise | late_entry | data_issue`).
- **Aggregates:** Spearman correlation of ex-ante score vs realized net return on the random
  stratum; failure-mode histogram weighted by |loss|.

### Stage 5 — Verdict and report

Apply the decision tree from the methodology doc (insufficient_data → simulation_artifact →
edge-destroyed-by-execution → no-edge sub-verdicts) with its default thresholds. Write the report
to `docs/verification-runs/<YYMMDD>-<run_id>.md`:

- verdict + confidence + headline numbers (including edge at both 2 h and the cards' stated
  horizon),
- findings table: all 14 suspects, confirmed/refuted/inconclusive, measured statistic, $ impact,
- configuration critique: every incoherent parameter pair and unvalidated default found, with
  the evidence and the concrete parameter change proposed,
- counterfactual table (baseline vs each child run, split-sample agreement),
- judge tables (ex-ante scores, failure-mode histogram, correlation),
- top generation recommendations and top trading recommendations, each traceable to a finding,
- appendix: integrity flags, excluded trades, caveats (sample size, 1-minute-bar bounds,
  multiple comparisons).

Commit the report. Then propose the single highest-impact change and offer to implement it. Any
implemented change must be validated on a **holdout window** (a fresh backtest on a window this
verification did not use) before being considered confirmed.

## Reporting

End the session with:

```
## verify-backtest: <run_id>

**Verdict:** <verdict> (<confidence>)
**Headline:** edge_gross <CI>, edge_net <CI>, top cause <suspect> ($<impact>)
**Report:** docs/verification-runs/<file>.md
**Recommended next change:** <one line>
```
