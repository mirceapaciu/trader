# Pipeline Judge — Audit Tooling Roadmap

This is a roadmap, not a component specification. It records the intended end state for the
system's development/audit tooling — a **Pipeline Judge** that verifies the complete workflow
from NewsFetcher through ThesisBuilder to TradeExecutor — and how the existing and planned audit
tools compose into it.

## 1. Purpose and Positioning

The Pipeline Judge is a family of operator-driven audit tools used for *developing* the
application: it identifies where in the workflow quality is lost (missed news, unsound analysis,
weak cards, bad trading rules, unfaithful execution) and proposes concrete fixes with measured
evidence. It is not a business component and is never part of the trading path.

Two execution styles coexist, chosen per module by what the work is:

- **In-app components** where the audit needs durable schemas, deterministic replay, or
  production-grade LLM batch runs (the Filter Quality Evaluator is the template).
- **Agent-driven skills** (Claude Code) where the audit is investigative and judgment-heavy —
  the agent can read code as well as data, adapt hypotheses to the evidence, and implement the
  fixes it recommends. Deterministic statistics used by a skill live in committed scripts, and
  simulation-time facts are pushed down into the owning component, so numbers stay reproducible.
  The backtest verification workflow is the template
  (`docs/design/backtest-verification-methodology.md` + `.agents/skills/verify-backtest/SKILL.md`).

## 2. Module Map

| Pipeline stage | Audit concern | Owning module | Status |
|---|---|---|---|
| NewsFetcher (filter) | Precision: were fetched articles correctly accepted/rejected? | Filter Quality Evaluator (`docs/design/product_components/filter-quality-evaluator/`) | **Exists** (component) |
| NewsFetcher (coverage) | Recall: was market-moving news missed entirely? | Future `news_coverage_auditor` | Not designed |
| Shared instrument lookup | Articles attributed to the wrong instrument | Systematize the `wrong_instrument` failure-mode findings from verification runs into a lookup-quality report | Future extension |
| ThesisBuilder (analysis) | Per-article reasoning soundness and impact quantification | `verify-backtest` skill — ex-ante analysis audit, suspect S13 (methodology §5) | **Designed** (skill) |
| ThesisBuilder (cards) | Ex-ante card soundness, failure modes, edge | `verify-backtest` skill — ex-ante card judging, suspects S6/S7 | **Designed** (skill) |
| Pipeline latency | Dollar cost of fetch/build delay | Backtester `both` scenario + suspect S8 | **Designed** |
| Trading rules | Exits, sizing, costs, horizon coherence; what each rule costs in P&L | `verify-backtest` skill — attribution script + counterfactual sweeps + configuration critique, suspects S1–S5, S12, S14 | **Designed** (skill) |
| Backtest itself | Data integrity, sim/live config parity | `verify-backtest` skill — Stage 1, suspects S10/S11; excursion/coverage facts persisted by the Backtester | **Designed** |
| TradeExecutor (live) | Fidelity: real fills/rejections/bracket behavior vs simulated expectations | Future `execution_reconciler` | Not designed |
| Whole workflow | One stage-attributed report: "stage X costs Y dollars of quality" | Future `pipeline_judge` orchestration skill | Not designed |

Supporting in-app changes owned by the Backtester: per-trade excursion metrics (MFE/MAE, horizon
returns, tie-break exposure, bar coverage) persisted at simulation time, and the future
`ExecutionModel` extensions (`bracket_mode`, `sizing_mode`, `min_confidence_to_trade`) for exact
live-parity counterfactuals.

## 3. Future Modules (sketches, one paragraph each)

**`news_coverage_auditor` (recall).** The Filter Quality Evaluator can only judge articles the
system fetched; nobody detects news that was never fetched. The auditor takes a window, pulls a
reference set of market-moving events for watchlist instruments from an external source
(candidates: a second news provider not used in production, exchange filings/press-release feeds,
or large price moves as event proxies — open question), and reports watchlist news the pipeline
never saw, attributed to provider coverage, polling cadence, or watchlist gaps. Prerequisite: a
reference source decision and its cost model.

**`execution_reconciler` (live fidelity).** Compares TradeExecutor's actual decisions, fills,
rejections, and bracket exits (`trade_executor` schema) against what the simulated rules say
should have happened for the same signals: admission-gate agreement, entry slippage vs the
backtester's assumption, bracket legs placed vs `construct_levels`, realized vs simulated exit
reasons. Likely a skill backed by a committed reconciliation script, following the
verify-backtest pattern. Prerequisite: enough live/paper execution history to be statistically
meaningful.

**`pipeline_judge` orchestration.** A skill (or thin CLI) that runs the Filter Quality Evaluator,
a `verify-backtest` verification, and the future modules over one shared window, then merges
their findings into a single stage-attributed report ranking cross-stage causes by measured
dollar impact. It adds no new analysis of its own.

## 4. Shared Conventions

All audit tooling shares, regardless of execution style: findings expressed as
suspect/hypothesis rows with a confirmed/refuted/inconclusive verdict, a measured statistic, and
a dollar-impact estimate; recommendations traceable to findings; out-of-sample validation of any
applied recommendation (holdout window); reports committed under `docs/verification-runs/`.
Deterministic statistics live in `scripts/verification/` and are reused across modules.

## 5. Sequencing

1. **Backtester excursion metrics + `verify-backtest` skill** (designed): answers the currently
   blocking question — why is backtest P&L negative — and covers the ThesisBuilder and
   trading-rule audit rows above.
2. **Verification-driven fixes**: apply recommendations, validate on holdout windows
   (methodology §8). This loop is expected to dominate development for a while.
3. **`execution_reconciler`**: once live/paper trading accumulates history — the natural next
   module because its findings gate scaling capital.
4. **`news_coverage_auditor`**: once a reference source is chosen; recall becomes the binding
   constraint only after precision and card quality are healthy.
5. **`pipeline_judge` orchestration**: last, once there are at least three modules worth merging.
