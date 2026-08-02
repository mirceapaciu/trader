<#!
.SYNOPSIS
Prints read-only diagnostics for the newest Haas Backtester run.
#>

$ErrorActionPreference = 'Stop'
$remoteQueries = @(
    @'
WITH latest AS (
  SELECT run_id FROM backtester.t_backtest_runs ORDER BY created_at DESC LIMIT 1
)
SELECT r.run_id, r.status, r.window_start_at, r.window_end_at, r.mode, r.timing_scenario,
       r.card_population, r.cards_considered, r.cards_in_population, r.cards_live_executable,
       r.cards_skipped_no_price, r.trades_opened, r.trades_closed
FROM backtester.t_backtest_runs r JOIN latest USING (run_id)
'@,
    @'
WITH latest AS (
  SELECT run_id FROM backtester.t_backtest_runs ORDER BY created_at DESC LIMIT 1
)
SELECT decision_state, count(*) AS cards, min(card_created_at) AS first_card,
       max(card_created_at) AS last_card
FROM backtester.t_backtest_card_snapshots JOIN latest USING (run_id)
GROUP BY decision_state ORDER BY decision_state
'@,
    @'
WITH latest AS (
  SELECT run_id FROM backtester.t_backtest_runs ORDER BY created_at DESC LIMIT 1
)
SELECT exit_reason, count(*) AS trades
FROM backtester.t_backtest_trades JOIN latest USING (run_id)
GROUP BY exit_reason ORDER BY exit_reason
'@
)

foreach ($query in $remoteQueries) {
    $oneLineQuery = ($query -replace "`r?`n", ' ') -replace '\s+', ' '
    $remoteCommand = "docker exec trader-postgres-1 psql -U trader -d trader -P pager=off -c '$oneLineQuery'"
    & ssh gh-runner_haas $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Haas query failed with exit code $LASTEXITCODE."
    }
}
