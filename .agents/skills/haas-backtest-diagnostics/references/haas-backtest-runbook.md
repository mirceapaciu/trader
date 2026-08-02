# Haas backtest runbook

Run all commands from `D:\src\trader` unless noted. These operations are read-only.

## Identify the run

```powershell
ssh gh-runner_haas "curl --fail --silent http://127.0.0.1:8090/api/backtests"
```

## Query a named run

Replace `<run_id>` only with the `bt_` ID returned by the API.

```bash
ssh gh-runner_haas "docker exec trader-postgres-1 psql -U trader -d trader -P pager=off -c 'SELECT run_id, status, window_start_at, window_end_at, mode, timing_scenario, card_population, cards_considered, cards_in_population, cards_live_executable, cards_skipped_no_price, trades_opened, trades_closed FROM backtester.t_backtest_runs WHERE run_id = $$<run_id>$$'"
```

```bash
ssh gh-runner_haas "docker exec trader-postgres-1 psql -U trader -d trader -P pager=off -c 'SELECT decision_state, count(*) AS cards, min(card_created_at) AS first_card, max(card_created_at) AS last_card FROM backtester.t_backtest_card_snapshots WHERE run_id = $$<run_id>$$ GROUP BY decision_state ORDER BY decision_state'"
```

```bash
ssh gh-runner_haas "docker exec trader-postgres-1 psql -U trader -d trader -P pager=off -c 'SELECT exit_reason, count(*) AS trades FROM backtester.t_backtest_trades WHERE run_id = $$<run_id>$$ GROUP BY exit_reason ORDER BY exit_reason'"
```

## Market-data evidence

Bound logs to the run interval. Include the surrounding traceback, not only the final error line.

```bash
ssh gh-runner_haas "docker logs --since <started_at> --until <finished_at> trader-monitoring-ui-1 2>&1 | grep -C 8 -E 'qualified_contract|Contract qualification failed|historical|prefetch complete|TimeoutError'"
```

The relevant path is `src/product_components/market_data/ibkr_gateway.py`:
`_qualified_contract()` logs and returns `None` after a timeout; `historical_bars()` then returns
an empty list. The engine counts the candidate under `cards_skipped_no_price` when no bar supplies
an entry price.

## Safe live confirmation

```bash
ssh gh-runner_haas "docker exec trader-monitoring-ui-1 .venv/bin/python -m src.product_components.diagnostics.ibkr_connectivity"
```

This test does not submit orders. It can time out and return non-zero when the Gateway accepts a
TCP/API connection but does not answer requests. Capture only the health fields and timeout
messages; do not print environment variables or credentials.

