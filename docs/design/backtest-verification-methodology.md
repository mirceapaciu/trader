# Backtest Verification Methodology

How to determine whether the news→thesis-card pipeline produces cards that make money, why a
backtest's P&L is what it is, and what to change first. This is the canonical reference for the
`verify-backtest` agent skill (`.agents/skills/verify-backtest/SKILL.md`), which executes this
methodology as a development/audit workflow.

**Execution model.** Verification is not an application component. It runs as an agent-driven
skill plus a small amount of committed code, split by nature:

- **Simulation-time facts belong to the Backtester.** Per-trade excursions (MFE/MAE), fixed-
  horizon returns, bar coverage, and tie-break exposure are computed during the simulation's own
  bar walk and persisted on `backtester.t_backtest_trades` (see the Backtester design docs).
- **Reproducible statistics belong to committed scripts.** `scripts/verification/attribution.py`
  computes the attribution slices and edge statistics defined here; numbers that feed a verdict
  are never improvised per session.
- **Judgment belongs to the agent.** Hypothesis-driven diagnosis, ex-ante card/analysis quality
  judging (the agent is the "expensive LLM judge"), code-level root-causing (an in-app judge
  cannot grep the code; the agent can), recommendation synthesis, and — after approval —
  implementing the fix and validating it on a holdout window.

The verification workflow is read-only toward all component schemas and writes only reports
(`docs/verification-runs/`) and committed scripts.

## 1. Verdicts

Every verification ends in exactly one verdict, with low/medium/high confidence:

1. **`cards_have_edge_execution_destroys_it`** — cards predict direction; exits/costs/latency
   turn gross edge into net losses. Recommendations target trading rules.
2. **`no_edge_discernible_by_judge`** — cards do not predict direction, and an independent
   ex-ante judge cannot separate good cards from bad either. Recommendations target the news
   pipeline and evidence-aggregation strategy.
3. **`no_edge_but_judge_discriminates`** — no average edge, but ex-ante judge scores correlate
   with outcomes: tighter generation gates can recover a profitable subset. Recommendations name
   the specific gates via the failure-mode ranking.
4. **`simulation_artifact`** — the P&L is (at least partly) an artifact of backtest configuration
   or data, not of the cards.
5. **`insufficient_data`** — too few closed trades for any defensible conclusion.

## 2. Stage 1 — Integrity

Trust the run before diagnosing it. Checks, with defaults:

- **Run completeness (regeneration runs):** the LLM token budget can exhaust mid-window while the
  run still finalizes `status = 'completed'` (issue 260708-01); the flag lives in
  `summary_json.regeneration.budget_exhausted`. When set, determine the covered sub-window (last
  analyzed article's `published_at`, from the run's sim schema) and scope every statistic and
  card-yield conclusion to it — a low card count over an uncovered window is a run artifact, not
  a pipeline property.
- Bar coverage per closed trade ≥ **0.95** (`bar_coverage_ratio` on the trade row); below ⇒ flag
  and exclude from edge statistics.
- Price sanity: no zero/negative/NaN prices, `high >= low`, monotone bar times in trade windows.
- No look-ahead (hard failure): `entry_at >= news_ready_at`; for actual-timing rows
  `entry_at >= card_created_at`.
- Survivorship pressure: `cards_skipped_no_price / cards_in_population` ≤ **0.2**; above ⇒ the
  sample is biased, note it on every conclusion.
- Population audit: share of trades from `rejected` / live-expired cards. Headline P&L over a
  population live trading would never execute (backtester default `card_population=all`) is an
  interpretation artifact, not a card defect.
- Config-parity audit vs live TradeExecutor rules (`construct_levels`: ATR stop + R-multiple
  target; `size_position`: `max_loss_usd / |entry − stop|`): bracket construction, sizing,
  minimum-confidence admission, population gating. Include generation outputs consumed by no gate
  (known: `price_impact_magnitude` persisted, never read).

**Configuration critique.** Every configured parameter in the card-generation and trading chain
is a hypothesis to challenge, not a given. Beyond sim/live parity, audit the parameters for
*internal coherence* — whether they are consistent with each other and with what the thesis
cards actually claim. Known incoherences to check first, then look for new ones:

Live-parity note: runs whose `execution_model_snapshot_json.mode` is `live_parity` invoke the
TradeExecutor pure pipeline for brackets, sizing, admission, risk gates, and time exits by
construction. For those runs, S11 audits only documented approximations: OHLCV bid/ask
approximation, assumed watchlist membership, and unknown historical sector exposure. For
`legacy_flat_percent`, run the full parity audit against live rules.

- **Horizon chain (S14):** card `time_horizon` (default `swing_1d_5d`) vs the execution time stop
  (4 h) vs card `expires_at` (creation + 6 h). A 1–5 *day* thesis that is force-closed within 4
  hours and expires in 6 is never actually tested at its own horizon; wins and losses inside 4 h
  say little about the thesis. Applies to both the backtest and live execution.
- **Target reachability:** take-profit % vs what the instrument can move within the time stop —
  share of trades whose MFE never came within half of the +3% target before the 4 h stop; a
  target that is rarely reachable inside the holding cap converts the strategy into
  "stop-loss or time-out."
- **Risk-box dead letters:** card `risk_max_loss_usd` ($120 default) and stop/invalidation
  conditions are hard-coded strings per strategy; the backtester's sizing ignores them entirely
  (confidence-fractional), the live executor uses `max_loss_usd` but not the textual conditions.
  Parameters that nothing enforces should be flagged as dead or wired up.
- **Evidence-window chain:** required evidence count (3 articles) within 120 min vs the 180 min
  staleness limit vs the card horizon — does the evidence cadence match the kind of thesis being
  claimed?
- **Threshold provenance:** for each numeric gate (min confidence, min relevance, ±% brackets,
  4 h stop), ask whether any measurement supports the value or whether it is an unvalidated
  default; unvalidated ones become counterfactual candidates.

Each incoherence found becomes a finding (usually under S11 or S14) with a recommendation and,
where an engine knob exists, a counterfactual that measures it.

## 3. Stage 2 — Attribution and edge

**Slices** (per `(dimension, bucket)`: n, wins, gross/net P&L, win rate, avg return):
`card_decision_state`, `card_was_live_expired`, strategy, direction, confidence bucket (edges
0.5/0.6/0.7/0.8/0.9), `exit_reason`, ticker (top |P&L| contributors), entry hour-of-day, evidence
count, `news_fetch_delay_seconds` decile, evidence source. Plus loss concentration (share of
total loss in worst k trades/tickers) and cost decomposition (gross vs commission vs slippage).

**Per-trade excursions** (persisted by the Backtester): `mfe_pct`, `mae_pct` with
times-to-extreme (±1-bar resolution; report as bounds when one bar holds both extremes);
fixed-horizon signed gross returns at 30 m / 1 h / 2 h / 4 h **and at day scale — 1 / 3 / 5
trading days — so a `swing_1d_5d` thesis is measured at the horizon it actually claims** (S14;
cost-free, exit-free — the purest card-signal measurement; day-scale horizons are null when the
run window ends first, so the window must extend past the last entry by the largest horizon);
post-exit return to card expiry; `both_brackets_in_one_bar` flag;
benchmark-adjusted return vs SPY over the holding period (skip, don't fail, if benchmark bars are
unavailable).

**Headline statistics** (over live-executable, clean-flag closed trades):
- `edge_gross`: bootstrap 95% CI (≥2000 iterations) of mean signed 2-hour horizon return +
  binomial test of the 2-hour hit rate vs 0.5. Computed **additionally at the card's stated
  horizon** (1-trading-day return for `swing_1d_5d` cards): a thesis population can have no
  2-hour edge and a real 1-day edge — that combination indicts the holding rules, not the cards.
- `edge_net`: bootstrap 95% CI of mean per-trade net return under the baseline config.

`edge_gross > 0 > edge_net` is the signature of "cards work, trading doesn't."

**Script contract** (`scripts/verification/attribution.py`): input `--run-id`, `--scenario
ideal|actual`; reads `backtester.t_backtest_runs/t_backtest_trades/t_backtest_card_snapshots`
read-only; outputs machine-readable JSON (all slices, both edge statistics with CI bounds and
sample counts) and a paste-ready Markdown summary; deterministic given a seed flag.

## 4. Stage 3 — Counterfactual sweeps

Measured causes, not speculation: re-run the Backtester once per factor via
`BacktesterService.run` with `dataclasses.replace` on the reconstructed baseline params.

**Generation-side thresholds are swept analytically first.** Regeneration runs persist their full
funnel in a surviving `sim_bt_<run_id>` schema (`t_news_analyses`, `t_evidence_windows`,
`t_thesis_cards`). Evidence-window width, required evidence count, and relevance/confidence gates
can be re-simulated over the persisted valid analyses at zero LLM cost; an engine re-run is then
spent only to confirm the selected configuration. Trading-side factors (costs, brackets, time
stops, tie-breaks) always require engine re-runs:

| Factor | Change | Tests |
|---|---|---|
| `approved_only` | `card_population=approved_only` | S1 |
| `zero_costs` | commissions + slippage = 0 | S2 |
| `optimistic_tiebreak` | `intrabar_stop_before_target=False` (bounds pair with baseline) | S3 |
| `bracket_grid` | `(take_profit_pct, stop_loss_pct)` ∈ {(2%,1%), (4%,2%), (1.5%,1.5%)} | S4 |
| `time_stop_grid` | `time_stop_seconds` ∈ {2 h, 8 h, 6 h card lifetime, **1 trading day, 3 trading days**} — the day-scale entries test the card's stated `swing_1d_5d` horizon | S5, S14 |
| `min_confidence_gate` | analytic: drop sub-0.6-confidence trades, re-sum (no engine knob; ignores portfolio interactions — say so) | S7 |

Discipline:
- **One factor per run**; no combined "best config" runs — sweeps are diagnostics, not tuning.
- Replay mode only; child runs tagged via `run_note`; `dataset_snapshot_hash` must match the
  baseline (population changes exempt, compared on the live-executable subset instead).
- **Split-sample sign agreement:** a factor is `confirmed` only if its improvement has the same
  sign over the first and second halves of the window; otherwise `inconclusive`. State the
  multiple-comparison caveat in the report.
- Current default: live-parity brackets/sizing/admission/risk gates are implemented through the
  shared pure TradeExecutor pipeline. Keep the grid sweeps for `legacy_flat_percent` runs and for
  exploratory counterfactuals, but do not report S11 as a rule-reimplementation gap for
  `live_parity` unless a documented approximation explains the difference.

## 5. Stage 4 — Ex-ante judging

The agent judges card and analysis quality independently of the generation model (gpt-4o-mini).

**Sampling:** from closed live-executable trades — 15 worst by net P&L, 10 best, 15 random;
stratum recorded; headline aggregates computed on the random stratum.

**Leakage safeguard (hard rule):** all ex-ante judgments are produced and written down before any
outcome data (entry/exit prices, excursions, returns) for the sampled cards is loaded. Ex-ante
inputs are only: the card (direction, strategy, confidence, evidence bullets, risk box,
created_at), its evidence articles (headline + summary), and pre-entry market context (prior
5-day return, overnight gap, time of day).

**Per card (ex-ante):** `soundness_score` 0–100 ("would a competent event-driven trader take
this?"), `direction_agreement` (agree/disagree/no_view), failure modes from:
`non_catalyst_opinion`, `stale_news`, `wrong_instrument`, `direction_unjustified`,
`over_aggregated_evidence`, `duplicate_evidence`, `magnitude_overstated`,
`market_wide_not_idiosyncratic`, `none`.

**Per analysis (ex-ante, up to 3 source analyses per sampled card)** — the multi-step magnitude
audit:
1. Concrete, datable event and its subject company.
2. Transmission channel (revenue/cost/margin/guidance, which direction) — "layoffs indicate
   operational challenges" is a channel claim, not a thesis.
3. Order-of-magnitude size vs company scale (estimated effect vs opex/market cap; hundreds of
   layoffs in one division of a multi-trillion-dollar company quantify to noise).
4. Consistency of the generation model's direction/confidence/`price_impact_magnitude` with that
   estimate.

Record `quantification_present` and analysis failure modes: `unquantified_impact`,
`channel_only_reasoning`, `magnitude_inconsistent`, `reasoning_ungrounded_in_article`,
`sentiment_direction_mismatch`, `event_misclassified`, `none`.

**Per card (ex-post, after outcomes are loaded):** `outcome_attribution` ∈ `thesis_wrong |
thesis_right_execution_wrong | market_noise | late_entry | data_issue`.

**Decisive aggregates:** Spearman ρ of ex-ante soundness vs realized net return (random stratum);
failure-mode histogram weighted by |loss|; direction-agreement rate by outcome. Judge
discrimination ⇒ generation gates can be tightened (the histogram names which); no discrimination
⇒ the defect is downstream of generation or not visible in the evidence.

## 6. The Suspect Catalog

Every verification reports all fourteen, each `confirmed | refuted | inconclusive` with its
statistic and a dollar-impact estimate.

| # | Suspect | Confirming evidence |
|---|---|---|
| S1 | Population contamination (rejected/stale cards in the backtest) | loss share in rejected/expired slices; `approved_only` delta > 0 |
| S2 | Cost drag | gross ≥ 0 > net; `zero_costs` delta ≈ total costs |
| S3 | Tie-break pessimism | high `both_brackets_in_one_bar` share; P&L sign flips between stop-first/target-first bounds ⇒ unresolvable at 1-minute bars |
| S4 | Bracket geometry (flat ±% vs the cards' move profiles / live ATR logic) | stopped-out losers with MFE ≥ +1–2% first; a grid cell better in both half-windows |
| S5 | Time stop (4 h arbitrary) | positive mean post-exit return after `time_stop` exits; grid delta |
| S6 | No directional edge | `edge_gross` CI includes or is below 0 |
| S7 | Uninformative confidence (mean-of-analyses; <0.6 still traded) | flat confidence-bucket P&L; rank corr ≈ 0; analytic gate delta |
| S8 | Latency | `pnl_gap`/`trades_flipped_by_delay` from `both`-scenario runs; hit-rate decay across delay deciles |
| S9 | Beta-riding (market moves, not news alpha) | benchmark-adjusted mean ≈ 0 while raw ≠ 0; `market_wide_not_idiosyncratic` frequency |
| S10 | Data-quality artifacts | Stage 1 hard violations or flagged share above threshold |
| S11 | Sim/live parity gap | for `live_parity`, differences beyond documented OHLCV/watchlist/sector approximations; for legacy runs, non-empty parity diff and associated counterfactual deltas |
| S12 | Reversal churn | reversal exits with positive forgone return; loss concentration in high-churn tickers |
| S13 | Unquantified analysis (qualitative reasoning, no magnitude estimate) | loss-weighted share of `unquantified_impact`/`channel_only_reasoning`; win rate worse for unquantified-backed trades |
| S14 | Horizon/config incoherence (thesis claims `swing_1d_5d`; time stop 4 h, expiry 6 h, targets sized for intraday) | day-scale `edge_gross` positive while 2 h edge ≈ 0; day-scale time-stop counterfactual delta > 0; target rarely reachable within the time stop; configuration-critique findings |

## 7. Verdict decision tree

Evaluated in order; defaults in bold:

1. `insufficient_data` — closed live-executable trades < **30**, or `edge_gross` CI wider than
   **2.0×** its midpoint magnitude.
2. `simulation_artifact` — hard integrity violations affect > **10%** of trades, or the
   stop-first/target-first P&L bounds straddle zero, or `approved_only` alone flips total P&L
   non-negative.
3. `cards_have_edge_execution_destroys_it` — `edge_gross` CI lower bound > 0 **at any measured
   horizon up to the card's stated horizon** (2 h or day-scale) while `edge_net` midpoint < 0,
   and the S2/S4/S5/S14 counterfactuals jointly recover ≥ **80%** of the gross-to-net gap. When
   only the day-scale edge is positive, the finding is specifically the horizon mismatch (S14):
   the cards work at the horizon they claim and the holding rules never let them get there.
4. Otherwise no average edge at any measured horizon; sub-verdict by judge discrimination:
   |ρ| ≥ **0.4** (p < 0.05) ⇒ `no_edge_but_judge_discriminates`, else
   `no_edge_discernible_by_judge`.

Confidence (low/medium/high) from sample size, CI widths, and agreement between the independent
evidence lines (attribution, counterfactuals, judge).

## 8. Closing the loop

Recommendations are hypotheses until validated **out-of-sample**: apply the change (ThesisBuilder
config/prompt for generation-side, run params for trading-side), run a fresh backtest on a
holdout window this verification did not use (regeneration mode for generation-side changes),
and re-verify. Never tune and validate on the same window.

## 9. Known limitations

- Fewer than ~30 closed trades makes most tests inconclusive — widen the window instead of
  lowering thresholds.
- 1-minute bars bound MFE/MAE and make intrabar ordering unknowable; when the tie-break bounds
  straddle zero, no exit-rule conclusion is safe at this granularity.
- The counterfactual matrix invites multiple-comparison errors; the split-sample rule mitigates
  but does not eliminate this.
- The analytic min-confidence estimate ignores portfolio interactions (cooldown, daily-loss
  limit, freed exposure).
- Ex-ante judging by the agent is a hindsight risk if the ordering rule is violated; the
  ex-ante-first, outcomes-after discipline is the control and must be observable in the session
  transcript/report.
- Regeneration-mode baselines: judging and attribution work (card snapshots are per-run), but
  counterfactual sweeps require a replay baseline.
- Day-scale horizons and day-scale time-stop counterfactuals need bars (and a run window)
  extending past the last card entry by up to 5 trading days; when they come back mostly null,
  widen the window rather than concluding from the intraday horizons alone.
