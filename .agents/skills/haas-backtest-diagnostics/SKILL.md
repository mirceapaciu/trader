---
name: haas-backtest-diagnostics
description: Diagnose why a completed Backtester run on Haas produced no trades or skipped thesis cards. Use for questions about the latest or named Haas backtest, especially zero-trade/zero-P&L runs, cards skipped for missing prices, IBKR historical-data failures, admission/risk blocks, or whether card generation versus execution caused the result.
---

# Haas Backtest Diagnostics

Use this read-only triage workflow before interpreting a Haas backtest's P&L or changing code.
For broad P&L, edge, or counterfactual analysis, also read and follow the repository's
`verify-backtest` skill and `docs/design/backtest-verification-methodology.md`.

## 1. Establish the run and its population

1. Follow the Haas read-only rules in `AGENTS.md`; use `ssh gh-runner_haas` and never start
   TradeExecutor, write to Postgres/Redis, or launch a counterfactual run unless requested.
2. Read `http://127.0.0.1:8090/api/backtests` on Haas to identify the latest completed `run_id`.
3. Run `scripts/inspect_latest_backtest.ps1` from the repository to print the latest run's stored
   metrics, decision-state card counts, and trade exit-reason counts. For a named run, use the
   same bounded queries in [references/haas-backtest-runbook.md](references/haas-backtest-runbook.md).
4. Treat `cards_considered` / `cards_in_population` as proof that cards were loaded into the
   simulation. Do not call a run "cardless" merely because it opened no trades.

## 2. Classify the zero-trade cause

Use the persisted counts before opening source code:

| Stored evidence | Meaning | Next check |
|---|---|---|
| `cards_in_population = 0` | No cards reached the simulation population. | Inspect the export/replay input and card filters. |
| `cards_skipped_no_price = cards_in_population` | Cards were loaded but no usable bar gave an entry price. | Inspect market-data prefetch logs and IBKR. |
| `risk_blocked` trades exist | Cards were priced but live-parity risk sizing/admission rejected them. | Slice by `risk_block_rule` and inspect the run snapshots. |
| Trades are `not_filled` | Entry limits were priced but never touched. | Inspect bar coverage and limit-order validity. |
| Trades opened then closed | The run used cards; follow `verify-backtest` for attribution. | Run the integrity checks before interpreting P&L. |

`cards_live_executable` is a card-status/timing count, not proof that historical bars were
available. A run can have live-executable cards and still skip every card for missing price.

## 3. Diagnose missing prices

1. Bound Monitoring UI logs to the run's `started_at` and `finished_at`.
2. Search for `Contract qualification failed`, `historical`, `prefetch complete`, and the affected
   ticker symbols. The Backtester prefetch runs inside `trader-monitoring-ui-1`.
3. A `Contract qualification failed for <ticker> (SMART/USD)` traceback ending in `TimeoutError`
   means `IbkrMarketDataGateway._qualified_contract()` returned no contract. Its
   `historical_bars()` method then returns `[]`, so the engine has no entry price and increments
   `cards_skipped_no_price`.
4. Run the non-trading probe below from `trader-monitoring-ui-1`:

   ```bash
   docker exec trader-monitoring-ui-1 .venv/bin/python -m src.product_components.diagnostics.ibkr_connectivity
   ```

   TCP connectivity and `api_connected=True` alone are insufficient. Timeouts from positions,
   orders, account updates, executions, or contract qualification establish that the IB Gateway
   API is connected but not servicing requests. Report that as an IBKR/Gateway responsiveness
   failure, not a ThesisBuilder failure.

## 4. Report precisely

State the run ID, card population, approved/rejected card split, skipped-price count, opened and
closed trades, and the decisive log/probe evidence. Explicitly label a 100% no-price run as a
simulation artifact: it says nothing about thesis-card quality or strategy P&L.

Do not claim a root cause beyond the available evidence. If requests are timing out now and did
so during the run, recommend restoring responsive IB Gateway API service and then rerunning the
same replay; do not mutate production services unless the user explicitly asks.

