import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  ApiError,
  fetchBacktestCards,
  fetchBacktestDetail,
  fetchBacktestEquity,
  fetchBacktestTrades,
  fetchBacktests,
  startBacktest,
  type BacktestCard,
  type BacktestCardTrade,
  type BacktestCardStatusBucket,
  type BacktestBlockedCandidateBreakdown,
  type BacktestDecisionDetails,
  type BacktestCardsResponse,
  type BacktestDelays,
  type BacktestDetailResponse,
  type BacktestEquityResponse,
  type BacktestMetrics,
  type BacktestRegenerationStats,
  type BacktestRunSummary,
  type BacktestStrategyMetrics,
  type BacktestTradeFilters,
  type BacktestTrade,
  type BacktestTradesResponse,
  type StartBacktestRequest,
  type ThroughputPresetWindow
} from "../api";

const WINDOW_PRESETS: Array<{ label: string; window: ThroughputPresetWindow }> = [
  { label: "15m", window: "15m" },
  { label: "1h", window: "1h" },
  { label: "1d", window: "1d" },
  { label: "7d", window: "7d" },
  { label: "30d", window: "30d" }
];

// Regeneration re-runs ThesisBuilder analysis with a chosen OpenAI model so runs
// can be compared across models. The default matches the production ThesisBuilder.
const DEFAULT_LLM_MODEL = "gpt-4o-mini";
const LLM_MODEL_OPTIONS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "o4-mini"];

const RUN_TERMINAL_STATES = new Set(["completed", "failed"]);

const TRADES_PAGE_SIZE = 50;

const CARD_BUCKET_LABELS: Record<string, string> = {
  approved: "Approved",
  rejected: "Rejected",
  card_was_live_expired: "Expired at entry",
  card_unexpired_at_entry: "Live-executable"
};

function runningBannerText(activeRunId: string, activeRun: BacktestRunSummary | null): string {
  const progress = activeRun?.progress ?? null;
  if (progress) {
    const ticker = progress.current_ticker ? ` (${progress.current_ticker})` : "";
    if (progress.phase === "regenerating") {
      const counter = progress.total > 0 ? ` ${progress.done}/${progress.total}` : "";
      return `Regenerating thesis cards${counter}${ticker}…`;
    }
    if (progress.phase === "prewarming") {
      return `Pre-warming market data ${progress.done}/${progress.total}${ticker}…`;
    }
    if (progress.phase === "simulating") {
      return "Running simulation…";
    }
  }
  return `Run ${activeRunId.slice(0, 8)}… is running. Wait for it to finish.`;
}

export function BacktesterTab() {
  const queryClient = useQueryClient();
  const [window, setWindow] = useState<ThroughputPresetWindow>("1d");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [submittedRunId, setSubmittedRunId] = useState<string | null>(null);

  const backtests = useQuery({
    queryKey: ["backtests", window],
    queryFn: () => fetchBacktests(window),
    // Poll faster while a run is active so pre-warming/simulation progress stays current.
    refetchInterval: (query) => (query.state.data?.active_run ? 5000 : 15000)
  });
  const data = backtests.data;
  const runs = data?.runs ?? [];
  const activeRun = data?.active_run ?? null;

  const startMutation = useMutation({
    mutationFn: (request: StartBacktestRequest) => startBacktest(request),
    onSuccess: (result) => {
      setSelectedRunId(result.run_id);
      setSubmittedRunId(result.run_id);
      queryClient.invalidateQueries({ queryKey: ["backtests"] });
    }
  });

  // Clear once the submitted run appears in the list with a terminal status.
  useEffect(() => {
    if (!submittedRunId) return;
    const found = runs.find((r) => r.run_id === submittedRunId);
    if (found && RUN_TERMINAL_STATES.has(found.status)) {
      setSubmittedRunId(null);
    }
  }, [runs, submittedRunId]);

  // Keep a sensible default selection without overriding operator choices.
  useEffect(() => {
    if (selectedRunId && runs.some((run) => run.run_id === selectedRunId)) {
      return;
    }
    if (runs.length > 0) {
      setSelectedRunId(runs[0].run_id);
    }
  }, [runs, selectedRunId]);

  // Local submission takes priority over the server-polled active_run so the
  // button stays disabled even when a fast run finishes before the next poll.
  const effectiveActiveRunId = submittedRunId ?? activeRun?.run_id ?? null;

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Backtester</p>
          <h1>Backtest Monitoring</h1>
        </div>
        <div className="timestamp">
          Last refresh
          <strong>{formatDate(data?.generated_at)}</strong>
        </div>
      </header>

      {backtests.isError && <div className="inline-error">{backtests.error.message}</div>}
      {data && !data.available && (
        <div className="inline-warning">{data.message ?? "Backtester telemetry unavailable."}</div>
      )}

      <TriggerPanel
        onSubmit={(request) => startMutation.mutate(request)}
        isSubmitting={startMutation.isPending}
        activeRunId={effectiveActiveRunId}
        error={startMutation.error}
        activeRun={activeRun}
      />

      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Window</h2>
            <span>Backtest runs by created time</span>
          </div>
        </div>
        <div className="window-toggle-group" aria-label="Backtest run time window">
          {WINDOW_PRESETS.map((preset) => (
            <button
              key={preset.window}
              type="button"
              className={window === preset.window ? "window-toggle active" : "window-toggle"}
              onClick={() => setWindow(preset.window)}
            >
              {preset.label}
            </button>
          ))}
        </div>
      </section>

      <RunListPanel
        runs={runs}
        available={data?.available ?? true}
        message={data?.message}
        selectedRunId={selectedRunId}
        onSelect={setSelectedRunId}
      />

      {selectedRunId ? <RunDetail runId={selectedRunId} /> : null}
    </main>
  );
}

function TriggerPanel({
  onSubmit,
  isSubmitting,
  activeRunId,
  error,
  activeRun
}: {
  onSubmit: (request: StartBacktestRequest) => void;
  isSubmitting: boolean;
  activeRunId: string | null;
  error: Error | null;
  activeRun: BacktestRunSummary | null;
}) {
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [mode, setMode] = useState<StartBacktestRequest["mode"]>("replay");
  const [llmModel, setLlmModel] = useState<string>(DEFAULT_LLM_MODEL);
  const [timingScenario, setTimingScenario] = useState<StartBacktestRequest["timing_scenario"]>("ideal");
  const [cardPopulation, setCardPopulation] = useState<StartBacktestRequest["card_population"]>("all");
  const [strategiesText, setStrategiesText] = useState("");
  const [initialCapitalText, setInitialCapitalText] = useState("");
  const [requiredEvidenceText, setRequiredEvidenceText] = useState("");
  const [evidenceWindowText, setEvidenceWindowText] = useState("");

  const conflict = error instanceof ApiError && error.status === 409;
  const invalid = error instanceof ApiError && error.status === 422;
  const busy = isSubmitting || activeRunId !== null;
  const canSubmit = windowStart !== "" && windowEnd !== "" && !busy;

  const submit = () => {
    if (!canSubmit) {
      return;
    }
    const strategies = strategiesText
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const initialCapital = initialCapitalText.trim() ? Number(initialCapitalText) : null;
    const isRegen = mode === "regeneration";
    const parseIntOrNull = (text: string): number | null => {
      const value = text.trim() ? Number(text) : NaN;
      return Number.isFinite(value) && value > 0 ? Math.trunc(value) : null;
    };
    onSubmit({
      window_start_at: localDateToIso(windowStart, "start"),
      window_end_at: localDateToIso(windowEnd, "end"),
      mode,
      timing_scenario: timingScenario,
      card_population: cardPopulation,
      strategies: strategies.length > 0 ? strategies : null,
      initial_capital: initialCapital != null && !Number.isNaN(initialCapital) ? initialCapital : null,
      run_note: null,
      llm_model: isRegen ? llmModel : null,
      required_evidence_count: isRegen ? parseIntOrNull(requiredEvidenceText) : null,
      evidence_collection_max_minutes: isRegen ? parseIntOrNull(evidenceWindowText) : null
    });
  };

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Run backtest</h2>
          <span>Bounded replay runs only — start long runs from the CLI</span>
        </div>
      </div>
      <div className="filter-editor">
        <div className="filter-editor-row backtest-window-row">
          <label>
            Window start (UTC)
            <input
              type="date"
              value={windowStart}
              onChange={(event) => setWindowStart(event.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Window end (UTC)
            <input
              type="date"
              value={windowEnd}
              onChange={(event) => setWindowEnd(event.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <div className="filter-editor-row">
          <label>
            Mode
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as StartBacktestRequest["mode"])}
              disabled={busy}
            >
              <option value="replay">replay</option>
              <option value="regeneration">regeneration</option>
            </select>
          </label>
          <label>
            LLM model (regeneration)
            <select
              value={llmModel}
              onChange={(event) => setLlmModel(event.target.value)}
              disabled={busy || mode !== "regeneration"}
            >
              {LLM_MODEL_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option === DEFAULT_LLM_MODEL ? `${option} (default)` : option}
                </option>
              ))}
            </select>
          </label>
          <label>
            Timing scenario
            <select
              value={timingScenario}
              onChange={(event) =>
                setTimingScenario(event.target.value as StartBacktestRequest["timing_scenario"])
              }
              disabled={busy}
            >
              <option value="ideal">ideal</option>
              <option value="actual">actual</option>
              <option value="both">both</option>
            </select>
          </label>
          <label>
            Card population
            <select
              value={cardPopulation}
              onChange={(event) =>
                setCardPopulation(event.target.value as StartBacktestRequest["card_population"])
              }
              disabled={busy}
            >
              <option value="all">all</option>
              <option value="approved_only">approved_only</option>
              <option value="rejected_only">rejected_only</option>
            </select>
          </label>
        </div>
        <div className="filter-editor-row">
          <label>
            Strategies (comma-separated, optional)
            <input
              value={strategiesText}
              onChange={(event) => setStrategiesText(event.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Initial capital (optional)
            <input
              type="number"
              min="0"
              step="any"
              value={initialCapitalText}
              onChange={(event) => setInitialCapitalText(event.target.value)}
              disabled={busy}
            />
          </label>
        </div>
        <div className="filter-editor-row">
          <label>
            Required evidence count (regeneration)
            <input
              type="number"
              min="1"
              step="1"
              placeholder="production default"
              value={requiredEvidenceText}
              onChange={(event) => setRequiredEvidenceText(event.target.value)}
              disabled={busy || mode !== "regeneration"}
            />
          </label>
          <label>
            Evidence window minutes (regeneration)
            <input
              type="number"
              min="1"
              step="1"
              placeholder="production default"
              value={evidenceWindowText}
              onChange={(event) => setEvidenceWindowText(event.target.value)}
              disabled={busy || mode !== "regeneration"}
            />
          </label>
        </div>
        {mode === "regeneration" && (
          <div className="inline-warning">
            Regeneration runs can be long and expensive. Prefer starting these from the CLI.
          </div>
        )}
        <div className="filter-editor-actions">
          <button type="button" className="primary-button" onClick={submit} disabled={!canSubmit}>
            {isSubmitting ? "Starting…" : activeRunId ? "Running…" : "Run backtest"}
          </button>
          {activeRunId !== null && !isSubmitting && (
            <span className="muted">{runningBannerText(activeRunId, activeRun)}</span>
          )}
          {!busy && (windowStart === "" || windowEnd === "") && (
            <span className="muted">
              Set a UTC date for both window start and end to enable this.
            </span>
          )}
        </div>
      </div>
      {conflict && (
        <div className="inline-warning">
          A backtest run is already active{activeRun ? `: ${activeRun.run_id}` : ""}. Wait for it to
          finish before starting another.
        </div>
      )}
      {invalid && <div className="inline-error">Invalid parameters: {error?.message}</div>}
      {error && !conflict && !invalid && <div className="inline-error">{error.message}</div>}
    </section>
  );
}

function RunListPanel({
  runs,
  available,
  message,
  selectedRunId,
  onSelect
}: {
  runs: BacktestRunSummary[];
  available: boolean;
  message?: string | null;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Recent Runs</h2>
          <span>{runs.length} run{runs.length === 1 ? "" : "s"}</span>
        </div>
      </div>
      {runs.length === 0 ? (
        <div className="empty">
          {!available ? message ?? "Backtester data unavailable" : "No backtest runs in this window."}
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Status</th>
                <th>Window</th>
                <th>Mode</th>
                <th>LLM</th>
                <th>Timing</th>
                <th>Population</th>
                <th>Net P&amp;L</th>
                <th>Total return</th>
                <th>Win rate</th>
                <th>Profit factor</th>
                <th>Max drawdown</th>
                <th>Created</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.run_id}
                  data-selectable
                  className={run.run_id === selectedRunId ? "selected" : undefined}
                  onClick={() => onSelect(run.run_id)}
                >
                  <td>
                    <strong>{run.run_id.slice(0, 8)}…</strong>
                  </td>
                  <td>
                    <span className={statusChipClass(run.status)}>{run.status}</span>
                    {run.budget_exhausted ? (
                      <span className="chip warning" title={budgetExhaustionNote(run)}>
                        {" "}
                        partial
                      </span>
                    ) : null}
                  </td>
                  <td>
                    {formatDate(run.window_start_at)}
                    <span className="table-subtext">to {formatDate(run.window_end_at)}</span>
                    {run.budget_exhausted ? (
                      <span className="table-subtext">{budgetExhaustionNote(run)}</span>
                    ) : null}
                  </td>
                  <td>{formatToken(run.mode)}</td>
                  <td>{run.llm_model ?? "—"}</td>
                  <td>{formatToken(run.timing_scenario)}</td>
                  <td>{formatToken(run.card_population)}</td>
                  <td>{formatCurrency(run.net_pnl)}</td>
                  <td>{formatPercent(run.total_return)}</td>
                  <td>{formatPercent(run.win_rate)}</td>
                  <td>{formatNumber(run.profit_factor)}</td>
                  <td>{formatPercent(run.max_drawdown)}</td>
                  <td>{formatDate(run.created_at)}</td>
                  <td>{formatDate(run.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const detail = useQuery({
    queryKey: ["backtests", "detail", runId],
    queryFn: () => fetchBacktestDetail(runId),
    refetchInterval: (query) =>
      RUN_TERMINAL_STATES.has(query.state.data?.run.status ?? "") ? false : 5000
  });
  const equity = useQuery({
    queryKey: ["backtests", "equity", runId],
    queryFn: () => fetchBacktestEquity(runId),
    refetchInterval: (query) =>
      RUN_TERMINAL_STATES.has(detail.data?.run.status ?? "") || query.state.data ? false : 5000
  });

  if (detail.isLoading) {
    return (
      <section className="panel">
        <div className="empty">Loading run detail…</div>
      </section>
    );
  }
  if (detail.isError) {
    return (
      <section className="panel">
        <div className="inline-error">{detail.error.message}</div>
      </section>
    );
  }
  const data = detail.data as BacktestDetailResponse | undefined;
  if (!data) {
    return null;
  }
  if (!data.available) {
    return (
      <section className="panel">
        <div className="inline-warning">{data.message ?? "Backtest run detail unavailable."}</div>
      </section>
    );
  }

  const isBoth = data.run.timing_scenario === "both";

  return (
    <>
      <SummaryTilesPanel run={data.run} metrics={data.metrics} />
      {data.regeneration ? <RegenerationPanel stats={data.regeneration} /> : null}
      <EquityPanel equity={equity.data} loading={equity.isLoading} error={equity.isError} />
      <PerStrategyPanel rows={data.per_strategy} />
      <CardStatusPanel rows={data.card_status_breakdown} />
      <DelaysPanel delays={data.delays} />
      <GapPanel run={data.run} gap={data.gap} isBoth={isBoth} />
      <CardsPanel runId={runId} />
      <TradesPanel
        runId={runId}
        metrics={data.metrics}
      />
    </>
  );
}

function SummaryTilesPanel({
  run,
  metrics
}: {
  run: BacktestRunSummary;
  metrics: BacktestMetrics;
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Run Summary</h2>
          <span>
            {run.run_id} · <span className={statusChipClass(run.status)}>{run.status}</span>
            {run.error_code ? ` · ${run.error_code}` : ""}
            {run.budget_exhausted ? (
              <span className="text-warning"> · ⚠ {budgetExhaustionNote(run)}</span>
            ) : null}
          </span>
        </div>
      </div>
      {run.status === "failed" ? (
        <div className="inline-error" role="alert">
          <div>{backtestFailureMessage(run)}</div>
          <MarketDataFailureDetails run={run} />
        </div>
      ) : null}
      <div className="thesis-kpi-grid">
        <MetricTile label="Total return" value={formatPercent(metrics.total_return)} />
        <MetricTile label="Net P&L" value={formatCurrency(metrics.net_pnl)} />
        <MetricTile label="Win rate" value={formatPercent(metrics.win_rate)} />
        <MetricTile label="Profit factor" value={formatNumber(metrics.profit_factor)} />
        <MetricTile label="Expectancy" value={formatCurrency(metrics.expectancy)} />
        <MetricTile label="Max drawdown" value={formatPercent(metrics.max_drawdown)} />
        <MetricTile label="Sharpe ratio" value={formatNumber(metrics.sharpe_ratio)} />
        <MetricTile label="# Trades" value={formatInteger(metrics.trades_closed)} />
        <MetricTile label="Exposure fraction" value={formatPercent(metrics.exposure_fraction)} />
        <MetricTile label="Signal accuracy" value={formatPercent(metrics.signal_accuracy)} />
      </div>
    </section>
  );
}

function backtestFailureMessage(run: BacktestRunSummary): string {
  const message = run.error_details?.message;
  if (typeof message === "string" && message.trim()) return message;
  if (run.error_code === "MarketDataUnavailableError") {
    return "Historical market data could not be received for the backtest.";
  }
  return `Backtest failed${run.error_code ? `: ${run.error_code}` : "."}`;
}

function MarketDataFailureDetails({ run }: { run: BacktestRunSummary }) {
  if (run.error_code !== "MarketDataUnavailableError") return null;
  const rawOutcomes = run.error_details?.unavailable_instruments;
  if (!Array.isArray(rawOutcomes)) return null;
  const outcomes = rawOutcomes.filter(
    (value): value is Record<string, unknown> => value !== null && typeof value === "object"
  );
  if (outcomes.length === 0) return null;

  return (
    <ul aria-label="Historical market data failure details">
      {outcomes.map((outcome, index) => {
        const ticker = stringField(outcome, "ticker") ?? "Unknown instrument";
        const exchange = stringField(outcome, "exchange_code");
        const provider = stringField(outcome, "provider");
        const category = stringField(outcome, "failure_category");
        const considered = Array.isArray(outcome.considered_providers)
          ? outcome.considered_providers.filter((value): value is string => typeof value === "string")
          : [];
        const errorCode = stringField(outcome, "error_code");
        const errorMessage = stringField(outcome, "error_message");
        const source = provider
          ? `Provider: ${provider}`
          : considered.length > 0
            ? `Considered: ${considered.join(", ")}`
            : "No eligible provider";
        const cause = marketDataFailureCategoryLabel(category);
        const providerError = [errorCode, errorMessage].filter(Boolean).join(": ");
        return (
          <li key={`${ticker}-${exchange ?? "unknown"}-${index}`}>
            <strong>{ticker}{exchange ? `/${exchange}` : ""}</strong>
            {` — ${source}; ${cause}${providerError ? ` (${providerError})` : ""}`}
          </li>
        );
      })}
    </ul>
  );
}

function stringField(value: Record<string, unknown>, key: string): string | null {
  const field = value[key];
  return typeof field === "string" && field.trim() ? field : null;
}

function marketDataFailureCategoryLabel(category: string | null): string {
  if (category === "no_provider_configured") return "No historical-data provider is configured";
  if (category === "no_symbol_mapping") return "No provider symbol mapping is available";
  if (category === "empty_response") return "The provider returned no bars for this window";
  if (category === "provider_error") return "The provider request failed";
  return "Historical data is unavailable";
}

function RegenerationPanel({ stats }: { stats: BacktestRegenerationStats }) {
  const tokensUsed = formatInteger(stats.llm_tokens_used);
  const tokenBudget = formatInteger(stats.llm_token_budget_limit);
  const coverage =
    stats.analysis_coverage_fraction == null
      ? stats.budget_exhausted
        ? "0%"
        : "100%"
      : `${Math.round(stats.analysis_coverage_fraction * 100)}%`;
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Regeneration</h2>
          <span>Article analysis funnel for this run</span>
        </div>
      </div>
      {stats.budget_exhausted ? (
        <div className="inline-warning compact">
          ⚠ Token budget exhausted — the window was not fully covered
          {stats.analysis_coverage_until_at
            ? ` (analysis covers through ${formatDate(stats.analysis_coverage_until_at)}, ${coverage} of window)`
            : ""}
          .
        </div>
      ) : null}
      <div className="thesis-kpi-grid">
        <MetricTile label="Articles in window" value={formatInteger(stats.articles_found)} />
        <MetricTile label="Articles relevant" value={formatInteger(stats.articles_relevant)} />
        <MetricTile label="Articles analyzed" value={formatInteger(stats.articles_analyzed)} />
        <MetricTile label="Analyses (LLM calls)" value={formatInteger(stats.analyses_created)} />
        <MetricTile label="Evidence windows" value={formatInteger(stats.evidence_windows_created)} />
        <MetricTile label="Cards created" value={formatInteger(stats.cards_created)} />
        <MetricTile label="LLM tokens used" value={`${tokensUsed} / ${tokenBudget}`} />
        <MetricTile label="Window coverage" value={coverage} />
      </div>
    </section>
  );
}

function EquityPanel({
  equity,
  loading,
  error
}: {
  equity?: BacktestEquityResponse;
  loading: boolean;
  error: boolean;
}) {
  const chartData = buildEquityChartData(equity);
  const hasIdeal = equity?.series.some((series) => series.timing_scenario === "ideal") ?? false;
  const hasActual = equity?.series.some((series) => series.timing_scenario === "actual") ?? false;

  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Equity Curve</h2>
          <span>Account equity over the run</span>
        </div>
      </div>
      {equity && !equity.available ? (
        <div className="inline-warning compact">{equity.message ?? "Equity data unavailable."}</div>
      ) : null}
      <div className="chart">
        {loading ? (
          <div className="empty">Loading equity curve…</div>
        ) : error ? (
          <div className="empty">Equity curve unavailable</div>
        ) : chartData.length > 0 ? (
          <ResponsiveContainer width="100%" height={290} minWidth={0}>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#dbe3dc" strokeDasharray="3 3" />
              <XAxis
                dataKey="timestampMs"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                tickLine={false}
                axisLine={false}
                minTickGap={20}
                tickFormatter={(value) => formatDate(new Date(Number(value)).toISOString())}
              />
              <YAxis tickLine={false} axisLine={false} />
              <Tooltip content={<EquityTooltip />} />
              {hasIdeal && (
                <Line type="monotone" dataKey="ideal" stroke="#1d8f6f" dot={false} connectNulls />
              )}
              {hasActual && (
                <Line type="monotone" dataKey="actual" stroke="#4967d1" dot={false} connectNulls />
              )}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">No equity points recorded for this run.</div>
        )}
      </div>
    </section>
  );
}

function PerStrategyPanel({ rows }: { rows: BacktestStrategyMetrics[] }) {
  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Per-Strategy Breakdown</h2>
          <span>Trade-level metrics per strategy</span>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="empty">No per-strategy metrics.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Blocked candidates</th>
                <th>Net P&amp;L</th>
                <th>Win rate</th>
                <th>Avg win</th>
                <th>Avg loss</th>
                <th>Profit factor</th>
                <th>Expectancy</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.strategy}>
                  <td>{formatToken(row.strategy)}</td>
                  <td>{formatInteger(row.trades_opened)}</td>
                  <td>{formatInteger(row.trades_closed)}</td>
                  <td>{formatInteger(row.trades_risk_blocked)}</td>
                  <td>{formatCurrency(row.net_pnl)}</td>
                  <td>{formatPercent(row.win_rate)}</td>
                  <td>{formatCurrency(row.avg_win)}</td>
                  <td>{formatCurrency(row.avg_loss)}</td>
                  <td>{formatNumber(row.profit_factor)}</td>
                  <td>{formatCurrency(row.expectancy)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CardStatusPanel({ rows }: { rows: BacktestCardStatusBucket[] }) {
  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Card-Status Breakdown</h2>
          <span>Metrics by thesis card population</span>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="empty">No card-status breakdown.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Bucket</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Blocked candidates</th>
                <th>Net P&amp;L</th>
                <th>Win rate</th>
                <th>Avg win</th>
                <th>Avg loss</th>
                <th>Profit factor</th>
                <th>Expectancy</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.bucket}>
                  <td>{CARD_BUCKET_LABELS[row.bucket] ?? formatToken(row.bucket)}</td>
                  <td>{formatInteger(row.trades_opened)}</td>
                  <td>{formatInteger(row.trades_closed)}</td>
                  <td>{formatInteger(row.trades_risk_blocked)}</td>
                  <td>{formatCurrency(row.net_pnl)}</td>
                  <td>{formatPercent(row.win_rate)}</td>
                  <td>{formatCurrency(row.avg_win)}</td>
                  <td>{formatCurrency(row.avg_loss)}</td>
                  <td>{formatNumber(row.profit_factor)}</td>
                  <td>{formatCurrency(row.expectancy)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DelaysPanel({ delays }: { delays: BacktestDelays }) {
  const rows: Array<{ label: string; avg?: number | null; p95?: number | null; max?: number | null }> = [
    {
      label: "News fetch delay",
      avg: delays.avg_news_fetch_delay_seconds,
      p95: delays.p95_news_fetch_delay_seconds,
      max: delays.max_news_fetch_delay_seconds
    },
    {
      label: "Thesis build delay",
      avg: delays.avg_thesis_build_delay_seconds,
      p95: delays.p95_thesis_build_delay_seconds,
      max: delays.max_thesis_build_delay_seconds
    },
    {
      label: "Total pipeline delay",
      avg: delays.avg_total_pipeline_delay_seconds,
      p95: delays.p95_total_pipeline_delay_seconds,
      max: delays.max_total_pipeline_delay_seconds
    }
  ];
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Pipeline Delays</h2>
          <span>News fetch, thesis build, and total pipeline</span>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Delay</th>
              <th>Avg</th>
              <th>P95</th>
              <th>Max</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td>{formatDuration(row.avg)}</td>
                <td>{formatDuration(row.p95)}</td>
                <td>{formatDuration(row.max)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function GapPanel({
  run,
  gap,
  isBoth
}: {
  run: BacktestRunSummary;
  gap?: BacktestDetailResponse["gap"];
  isBoth: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Ideal vs Actual Gap</h2>
          <span>Impact of pipeline delay on results</span>
        </div>
      </div>
      {!isBoth ? (
        <div className="empty">
          Gap metrics require a <strong>both</strong> timing scenario. This run used{" "}
          {formatToken(run.timing_scenario)}.
        </div>
      ) : (
        <div className="thesis-kpi-grid">
          <MetricTile label="P&L gap" value={formatCurrency(gap?.pnl_gap)} />
          <MetricTile label="Win rate gap" value={formatPercent(gap?.win_rate_gap)} />
          <MetricTile label="Trades flipped by delay" value={formatInteger(gap?.trades_flipped_by_delay)} />
        </div>
      )}
    </section>
  );
}

function CardsPanel({ runId }: { runId: string }) {
  const cards = useQuery({
    queryKey: ["backtests", "cards", runId],
    queryFn: () => fetchBacktestCards(runId)
  });
  const data = cards.data as BacktestCardsResponse | undefined;
  const rows = data?.cards ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedDecision, setSelectedDecision] = useState<BacktestCardTrade | null>(null);
  const selectedCard = rows.find((card) => card.thesis_card_id === selectedId) ?? null;

  // Reset the selection when switching runs so we never show a stale card.
  useEffect(() => {
    setSelectedId(null);
    setSelectedDecision(null);
  }, [runId]);

  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Cards</h2>
          <span>{rows.length} card{rows.length === 1 ? "" : "s"}</span>
        </div>
      </div>
      {cards.isError && <div className="inline-error">{cards.error.message}</div>}
      {data && !data.available ? (
        <div className="inline-warning compact">{data.message ?? "Card data unavailable."}</div>
      ) : null}
      {rows.length === 0 ? (
        <div className="empty">{cards.isLoading ? "Loading cards…" : "No cards for this run."}</div>
      ) : (
        <div className="pending-windows-layout">
          <div className="pending-list-wrap cards-list-wrap">
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Strategy</th>
                  <th>Direction</th>
                  <th>Confidence</th>
                  <th>Decision</th>
                  <th>Candidates</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((card) => (
                  <tr
                    key={card.thesis_card_id}
                    data-selectable
                    className={card.thesis_card_id === selectedId ? "selected" : undefined}
                    onClick={() => setSelectedId(card.thesis_card_id)}
                  >
                    <td>
                      <strong>{card.ticker}</strong>
                      <span className="table-subtext">{card.exchange_code}</span>
                    </td>
                    <td>{formatToken(card.strategy)}</td>
                    <td>{formatToken(card.direction)}</td>
                    <td>{card.confidence != null ? `${(card.confidence * 100).toFixed(0)}%` : "—"}</td>
                    <td>{formatToken(card.decision_state)}</td>
                    <td>{card.trades.length}</td>
                    <td>{formatDate(card.card_created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pending-detail">
            {selectedCard ? (
              <CardDetail card={selectedCard} onExplain={setSelectedDecision} />
            ) : (
              <div className="empty">Select a card to see details.</div>
            )}
          </div>
        </div>
      )}
      {selectedDecision ? (
        <DecisionDrawer trade={selectedDecision} onClose={() => setSelectedDecision(null)} />
      ) : null}
    </section>
  );
}

function CardDetail({
  card,
  onExplain
}: {
  card: BacktestCard;
  onExplain: (trade: BacktestCardTrade) => void;
}) {
  return (
    <div className="pending-detail-grid">
      <div className="pending-detail-row">
        <span>Card ID</span>
        <strong style={{ fontSize: "0.8rem", wordBreak: "break-all" }}>{card.thesis_card_id}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Instrument</span>
        <strong>
          {card.ticker} <small style={{ fontWeight: 400, color: "#64726c" }}>{card.exchange_code}</small>
        </strong>
      </div>
      <div className="pending-detail-row">
        <span>Strategy</span>
        <strong>{formatToken(card.strategy)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Direction</span>
        <strong>{formatToken(card.direction)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Time horizon</span>
        <strong>{card.time_horizon ? formatToken(card.time_horizon) : "—"}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Confidence</span>
        <strong>{card.confidence != null ? `${(card.confidence * 100).toFixed(0)}%` : "—"}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Decision</span>
        <strong>{formatToken(card.decision_state)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Created</span>
        <strong>{formatDate(card.card_created_at)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Expires</span>
        <strong>{card.card_expires_at ? formatDate(card.card_expires_at) : "—"}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Candidate outcomes</span>
        <strong>{card.trades.length} candidate{card.trades.length === 1 ? "" : "s"}</strong>
      </div>
      {card.trades.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Timing</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Net P&amp;L</th>
                <th>Return</th>
                <th>Outcome</th>
                <th>Block reason</th>
              </tr>
            </thead>
            <tbody>
              {card.trades.map((trade) => (
                <tr key={trade.trade_id} id={tradeAnchorId(trade.trade_id)}>
                  <td>{formatToken(trade.entry_timing_scenario)}</td>
                  <td>
                    {formatDate(trade.entry_at)}
                    <span className="table-subtext">{formatNumber(trade.entry_price)}</span>
                  </td>
                  <td>
                    {formatDate(trade.exit_at)}
                    <span className="table-subtext">{formatNumber(trade.exit_price)}</span>
                  </td>
                  <td>{formatCurrency(trade.net_pnl)}</td>
                  <td>{formatPercent(trade.return_pct)}</td>
                  <td>{trade.exit_reason ? outcomeLabel(trade.exit_reason) : "—"}</td>
                  <td>
                    {decisionReason(trade) ? (
                      <button type="button" className="table-link-button" onClick={() => onExplain(trade)}>
                        {formatToken(decisionReason(trade)!)}
                      </button>
                    ) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TradesPanel({
  runId,
  metrics
}: {
  runId: string;
  metrics: BacktestMetrics;
}) {
  const [filters, setFilters] = useState<{
    timing_scenario: string;
    strategy: string;
    exit_reason: string;
    card_status: string;
    decision_stage: string;
    decision_reason: string;
  }>({
    timing_scenario: "",
    strategy: "",
    exit_reason: "",
    card_status: "",
    decision_stage: "",
    decision_reason: ""
  });
  const [offset, setOffset] = useState(0);
  const [selectedDecision, setSelectedDecision] = useState<BacktestTrade | null>(null);

  // Reset pagination when the run or filters change.
  useEffect(() => {
    setOffset(0);
  }, [runId, filters]);

  const queryFilters: BacktestTradeFilters = {
    timing_scenario: filters.timing_scenario || undefined,
    strategy: filters.strategy || undefined,
    exit_reason: filters.exit_reason || undefined,
    card_status: filters.card_status || undefined,
    decision_stage: filters.decision_stage || undefined,
    decision_reason: filters.decision_reason || undefined,
    limit: TRADES_PAGE_SIZE,
    offset
  };
  const trades = useQuery({
    queryKey: ["backtests", "trades", runId, queryFilters],
    queryFn: () => fetchBacktestTrades(runId, queryFilters)
  });
  const data = trades.data as BacktestTradesResponse | undefined;
  const rows = data?.trades ?? [];
  const totalCount = data?.total_count ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + TRADES_PAGE_SIZE < totalCount;
  const blockedCandidates = data?.blocked_candidate_breakdown ?? null;

  const updateFilter = (key: keyof typeof filters, value: string) =>
    setFilters((current) => ({ ...current, [key]: value }));

  const applyDecisionFilter = (stage: string, reason = "") => {
    setFilters((current) => ({ ...current, decision_stage: stage, decision_reason: reason }));
  };

  return (
    <>
      <BlockedCandidatesPanel
        breakdown={blockedCandidates}
        metrics={metrics}
        selectedStage={filters.decision_stage}
        selectedReason={filters.decision_reason}
        onSelect={applyDecisionFilter}
      />
      <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Trades and candidates</h2>
          <span>{totalCount} row{totalCount === 1 ? "" : "s"}</span>
        </div>
      </div>
      <div className="filter-editor-row">
        <label>
          Timing scenario
          <input
            value={filters.timing_scenario}
            onChange={(event) => updateFilter("timing_scenario", event.target.value)}
            placeholder="ideal / actual"
          />
        </label>
        <label>
          Strategy
          <input
            value={filters.strategy}
            onChange={(event) => updateFilter("strategy", event.target.value)}
          />
        </label>
        <label>
          Outcome code
          <input
            value={filters.exit_reason}
            onChange={(event) => updateFilter("exit_reason", event.target.value)}
          />
        </label>
        <label>
          Card status
          <input
            value={filters.card_status}
            onChange={(event) => updateFilter("card_status", event.target.value)}
          />
        </label>
        <label>
          Decision stage
          <select
            aria-label="Decision stage"
            value={filters.decision_stage}
            onChange={(event) => applyDecisionFilter(event.target.value)}
          >
            <option value="">All stages</option>
            {decisionStages(blockedCandidates).map((stage) => (
              <option value={stage} key={stage}>{decisionStageLabel(stage)}</option>
            ))}
          </select>
        </label>
        <label>
          Block reason
          <select
            aria-label="Block reason"
            value={filters.decision_reason}
            onChange={(event) => updateFilter("decision_reason", event.target.value)}
          >
            <option value="">All reasons</option>
            {decisionReasons(blockedCandidates, filters.decision_stage).map((reason) => (
              <option value={reason} key={reason}>{formatToken(reason)}</option>
            ))}
          </select>
        </label>
      </div>
      {trades.isError && <div className="inline-error">{trades.error.message}</div>}
      {data && !data.available ? (
        <div className="inline-warning compact">{data.message ?? "Trade data unavailable."}</div>
      ) : null}
      {rows.length === 0 ? (
        <div className="empty">{trades.isLoading ? "Loading trades…" : "No trades match these filters."}</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Strategy</th>
                <th>Direction</th>
                <th>Timing</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Net P&amp;L</th>
                <th>Return</th>
                <th>Outcome</th>
                <th>Decision</th>
                <th>News delay</th>
                <th>Thesis delay</th>
                <th>Pipeline delay</th>
                <th>Card status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((trade) => (
                <tr key={trade.trade_id}>
                  <td>
                    <strong>{trade.ticker}</strong>
                    <span className="table-subtext">{trade.exchange_code}</span>
                  </td>
                  <td>{formatToken(trade.strategy)}</td>
                  <td>{formatToken(trade.direction)}</td>
                  <td>{formatToken(trade.entry_timing_scenario)}</td>
                  <td>
                    {formatDate(trade.entry_at)}
                    <span className="table-subtext">{formatNumber(trade.entry_price)}</span>
                  </td>
                  <td>
                    {formatDate(trade.exit_at)}
                    <span className="table-subtext">{formatNumber(trade.exit_price)}</span>
                  </td>
                  <td>{formatCurrency(trade.net_pnl)}</td>
                  <td>{formatPercent(trade.return_pct)}</td>
                  <td>{trade.exit_reason ? outcomeLabel(trade.exit_reason) : "—"}</td>
                  <td>
                    {decisionReason(trade) ? (
                      <button
                        type="button"
                        className="table-link-button"
                        onClick={() => setSelectedDecision(trade)}
                      >
                        {decisionStageLabel(trade.decision_stage)} · {formatToken(decisionReason(trade)!)}
                      </button>
                    ) : "—"}
                  </td>
                  <td>{formatDuration(trade.news_fetch_delay_seconds)}</td>
                  <td>{formatDuration(trade.thesis_build_delay_seconds)}</td>
                  <td>{formatDuration(trade.total_pipeline_delay_seconds)}</td>
                  <td>{trade.card_decision_state ? formatToken(trade.card_decision_state) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="filter-editor-actions">
        <button
          type="button"
          className="secondary-button"
          onClick={() => setOffset((current) => Math.max(0, current - TRADES_PAGE_SIZE))}
          disabled={!hasPrev}
        >
          Previous
        </button>
        <span className="throughput-helper">
          {totalCount === 0
            ? "0 of 0"
            : `${offset + 1}–${Math.min(offset + TRADES_PAGE_SIZE, totalCount)} of ${totalCount}`}
        </span>
        <button
          type="button"
          className="secondary-button"
          onClick={() => setOffset((current) => current + TRADES_PAGE_SIZE)}
          disabled={!hasNext}
        >
          Next
        </button>
      </div>
      {selectedDecision ? (
        <DecisionDrawer trade={selectedDecision} onClose={() => setSelectedDecision(null)} />
      ) : null}
      </section>
    </>
  );
}

function BlockedCandidatesPanel({
  breakdown,
  metrics,
  selectedStage,
  selectedReason,
  onSelect
}: {
  breakdown: BacktestBlockedCandidateBreakdown | null;
  metrics: BacktestMetrics;
  selectedStage: string;
  selectedReason: string;
  onSelect: (stage: string, reason?: string) => void;
}) {
  const total = breakdown?.total ?? metrics.trades_risk_blocked ?? 0;
  const decidedCandidateTotal = metrics.trades_opened == null ? null : metrics.trades_opened + total;
  const denominator = breakdown?.candidate_total ?? decidedCandidateTotal ?? metrics.cards_considered ?? null;
  const serverPercentage = breakdown?.percentage;
  const percentage = serverPercentage != null
    ? serverPercentage <= 1 ? serverPercentage * 100 : serverPercentage
    : denominator && denominator > 0 ? total / denominator * 100 : null;

  return (
    <section className="panel panel-large blocked-candidates-panel">
      <div className="panel-heading">
        <div>
          <h2>Blocked candidates</h2>
          <span>Candidates that were not entered, grouped by the stage that made the decision</span>
        </div>
      </div>
      <div className="blocked-candidates-summary">
        <div className="metric">
          <span>Blocked candidates</span>
          <strong>{formatInteger(total)}</strong>
        </div>
        <div className="metric">
          <span>Share of candidates</span>
          <strong>{percentage == null ? "—" : `${percentage.toFixed(1)}%`}</strong>
        </div>
      </div>
      {breakdown?.by_stage.length ? (
        <div className="blocked-breakdown" aria-label="Blocked candidates by stage and reason">
          <button
            type="button"
            className={!selectedStage && !selectedReason ? "blocked-bucket active" : "blocked-bucket"}
            onClick={() => onSelect("", "")}
          >
            <span>All blocked candidates</span><strong>{total}</strong>
          </button>
          {breakdown.by_stage.map((stage) => (
            <div className="blocked-stage" key={stage.stage}>
              <button
                type="button"
                className={selectedStage === stage.stage && !selectedReason ? "blocked-bucket active" : "blocked-bucket"}
                onClick={() => onSelect(stage.stage, "")}
              >
                <span>{decisionStageLabel(stage.stage)}</span><strong>{stage.count}</strong>
              </button>
              <div className="blocked-reasons">
                {stage.reasons.map((reason) => (
                  <button
                    type="button"
                    key={`${stage.stage}:${reason.reason}`}
                    className={selectedStage === stage.stage && selectedReason === reason.reason ? "blocked-reason active" : "blocked-reason"}
                    onClick={() => onSelect(stage.stage, reason.reason)}
                  >
                    <span>{formatToken(reason.reason)}</span><strong>{reason.count}</strong>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : total > 0 ? (
        <div className="inline-warning compact">
          A stage and reason breakdown was not recorded for this run.
        </div>
      ) : (
        <div className="empty">No blocked candidates in this run.</div>
      )}
    </section>
  );
}

type DecisionTrade = Pick<
  BacktestTrade,
  "trade_id" | "decision_at" | "decision_stage" | "decision_reason" | "decision_details_json" | "decision_details_message" | "risk_block_rule"
>;

function DecisionDrawer({ trade, onClose }: { trade: DecisionTrade; onClose: () => void }) {
  const details = recordValue(trade.decision_details_json);
  const reason = decisionReason(trade);
  const legacy = !trade.decision_stage && !details && Boolean(trade.risk_block_rule);
  const comparison = recordValue(details?.comparison);
  const observed = comparison?.observed ?? details?.observed;
  const threshold = comparison?.expected ?? details?.threshold ?? details?.expected;
  const comparator = stringValue(comparison?.operator) ?? stringValue(details?.comparator);
  const condition = details?.condition;

  return (
    <div className="taxonomy-drawer-backdrop decision-drawer-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <aside className="taxonomy-drawer decision-drawer" role="dialog" aria-modal="true" aria-label="Blocked candidate explanation">
        <div className="modal-header">
          <div>
            <p className="eyebrow">Candidate decision</p>
            <h3>{reason ? formatToken(reason) : "Not entered"}</h3>
          </div>
          <button type="button" className="modal-close" aria-label="Close candidate explanation" onClick={onClose}>×</button>
        </div>
        <div className="taxonomy-drawer-body decision-drawer-body">
          <dl className="taxonomy-gap-facts">
            <DecisionFact label="Stage" value={decisionStageLabel(trade.decision_stage)} />
            <DecisionFact label="Reason code" value={reason ?? "Not recorded"} code />
            <DecisionFact label="Check" value={stringValue(details?.check_id) ?? stringValue(details?.failed_check) ?? "Not recorded"} code />
            <DecisionFact label="Decided" value={formatDate(trade.decision_at)} />
          </dl>

          {legacy ? (
            <div className="inline-warning" role="note">
              {trade.decision_details_message ?? "This is a legacy result. Detailed operands were not recorded for this run; only the block reason code is available."}
            </div>
          ) : (
            <p className="decision-explanation">
              {decisionExplanation(trade.decision_stage, reason, stringValue(details?.check_id))}
            </p>
          )}

          {condition != null || observed != null || threshold != null ? (
            <section className="decision-section">
              <h4>Failed comparison</h4>
              {condition != null ? <p>{formatDecisionValue(condition)}</p> : null}
              {observed != null || threshold != null ? (
                <div className="decision-comparison">
                  <span><small>{operandLabel(observed, "Observed")}</small>{formatDecisionValue(observed)}</span>
                  <strong>{comparatorSymbol(comparator)}</strong>
                  <span><small>{operandLabel(threshold, "Required threshold")}</small>{formatDecisionValue(threshold)}</span>
                </div>
              ) : null}
            </section>
          ) : null}

          <DecisionValueSection title="Inputs" values={details?.inputs} />
          <DecisionValueSection title="Derived values" values={details?.derived_values} />
          {details?.binding_constraint != null ? (
            <section className="decision-section">
              <h4>Binding constraint</h4>
              <p>{formatDecisionValue(details.binding_constraint)}</p>
            </section>
          ) : null}
          <RelatedRecords value={details?.related_records ?? details?.related_record} />
          {details ? (
            <p className="decision-schema-version">
              Explanation schema {formatDecisionValue(details.schema_version ?? "unknown")}
            </p>
          ) : !legacy ? (
            <div className="inline-warning compact">No structured decision operands were recorded.</div>
          ) : null}
        </div>
      </aside>
    </div>
  );
}

function DecisionFact({ label, value, code = false }: { label: string; value: string; code?: boolean }) {
  return <div><dt>{label}</dt><dd className={code ? "decision-code" : undefined}>{value}</dd></div>;
}

function DecisionValueSection({ title, values }: { title: string; values: unknown }) {
  const record = recordValue(values);
  if (!record || Object.keys(record).length === 0) return null;
  return (
    <section className="decision-section">
      <h4>{title}</h4>
      <dl className="decision-values">
        {Object.entries(record).map(([key, value]) => (
          <div key={key}><dt>{formatToken(key)}</dt><dd>{formatDecisionValue(value)}</dd></div>
        ))}
      </dl>
    </section>
  );
}

function RelatedRecords({ value }: { value: unknown }) {
  const records = Array.isArray(value) ? value : value == null ? [] : [value];
  if (records.length === 0) return null;
  return (
    <section className="decision-section">
      <h4>Related records</h4>
      <ul className="decision-related-records">
        {records.map((item, index) => {
          const record = recordValue(item);
          const label = record
            ? stringValue(record.label) ?? [
                stringValue(record.record_type) ?? stringValue(record.type),
                record.id != null ? formatDecisionValue(record.id) : record.record_id != null ? formatDecisionValue(record.record_id) : null
              ].filter(Boolean).join(" ")
            : formatDecisionValue(item);
          const href = record ? stringValue(record.href) ?? stringValue(record.url) : null;
          const recordType = record
            ? stringValue(record.record_type) ?? stringValue(record.type)
            : null;
          const recordId = record
            ? stringValue(record.record_id) ?? stringValue(record.id)
            : null;
          const relatedHref = href ?? (
            recordType === "simulated_trade" && recordId
              ? `#${tradeAnchorId(recordId)}`
              : null
          );
          return <li key={`${label}-${index}`}>{relatedHref ? <a href={relatedHref}>{label || "Open related record"}</a> : label || "Related record"}</li>;
        })}
      </ul>
    </section>
  );
}

function tradeAnchorId(tradeId: string): string {
  return `backtest-trade-${encodeURIComponent(tradeId)}`;
}

function decisionReason(trade: Pick<DecisionTrade, "decision_reason" | "risk_block_rule">): string | null {
  return trade.decision_reason || trade.risk_block_rule || null;
}

function decisionStageLabel(stage?: string | null): string {
  if (stage === "admission") return "Admission";
  if (stage === "market_data") return "Market data";
  if (stage === "sizing") return "Sizing";
  if (stage === "portfolio_risk") return "Portfolio risk";
  return stage ? formatToken(stage) : "Not recorded";
}

function decisionExplanation(
  stage: string | null | undefined,
  reason: string | null,
  checkId: string | null
): string {
  const checks: Record<string, string> = {
    "portfolio_risk.max_positions": "The candidate was not entered because the portfolio had reached its open-position limit.",
    "portfolio_risk.max_portfolio_exposure": "The candidate was not entered because the proposed position would exceed the portfolio exposure limit.",
    "portfolio_risk.max_sector_exposure": "The candidate was not entered because the proposed position would exceed the sector exposure limit.",
    "portfolio_risk.daily_loss_limit": "The candidate was not entered because combined daily P&L newly triggered the daily-loss guardrail.",
    "portfolio_risk.daily_loss_halt_latched": "The candidate was not entered because the daily-loss guardrail had already latched earlier that trading day."
  };
  if (checkId && checks[checkId]) return checks[checkId];
  const known: Record<string, string> = {
    review_not_approved: "The candidate was not entered because its recorded review state did not satisfy the admission requirement.",
    card_expired: "The candidate was not entered because the thesis card had expired before the attempted entry.",
    below_min_confidence: "The candidate was not entered because its confidence was below the configured admission threshold.",
    not_in_watchlist: "The candidate was not entered because its instrument was not on the eligible watchlist.",
    horizon_unmapped: "The candidate was not entered because its time horizon could not be mapped to an execution duration.",
    atr_unavailable: "The candidate was not entered because the ATR market-data input required for sizing was unavailable.",
    size_below_one_share: "The candidate was not entered because the calculated position size was below one whole share.",
    position_exists: "The candidate was not entered because a position for this instrument already existed.",
    confidence_below_threshold: "The candidate was not entered because its confidence was below the configured admission threshold.",
    portfolio_cap_exceeded: "The candidate was not entered because the proposed position would exceed the portfolio exposure limit.",
    sector_cap_exceeded: "The candidate was not entered because the proposed position would exceed the sector exposure limit.",
    max_positions: "The candidate was not entered because the portfolio had reached its open-position limit.",
    max_daily_trades: "The candidate was not entered because the daily trade-count limit had been reached.",
    max_daily_trades_reached: "The candidate was not entered because the daily trade-count limit had been reached.",
    daily_loss_halt: "The candidate was not entered because the daily-loss guardrail was active."
  };
  if (reason && known[reason]) return known[reason];
  if (reason) return `The candidate was not entered at the ${decisionStageLabel(stage).toLowerCase()} stage. This UI does not recognize the future reason code “${reason}”; the recorded details are shown below.`;
  return "The candidate was not entered, but no stable reason code was recorded.";
}

function decisionStages(breakdown: BacktestBlockedCandidateBreakdown | null): string[] {
  return breakdown?.by_stage.map((bucket) => bucket.stage) ?? [];
}

function decisionReasons(breakdown: BacktestBlockedCandidateBreakdown | null, stage: string): string[] {
  const stages = stage ? breakdown?.by_stage.filter((bucket) => bucket.stage === stage) : breakdown?.by_stage;
  return Array.from(new Set((stages ?? []).flatMap((bucket) => bucket.reasons.map((reason) => reason.reason))));
}

function outcomeLabel(reason: string): string {
  return reason === "risk_blocked" ? "Not entered" : formatToken(reason);
}

function comparatorSymbol(comparator: string | null): string {
  const symbols: Record<string, string> = {
    lt: "<", lte: "≤", gt: ">", gte: "≥", eq: "=", ne: "≠",
    less_than: "<", less_than_or_equal: "≤", greater_than: ">", greater_than_or_equal: "≥",
    equals: "=", not_equals: "≠"
  };
  return comparator ? symbols[comparator] ?? comparator : "compared with";
}

function operandLabel(value: unknown, fallback: string): string {
  const operand = recordValue(value);
  return operand ? stringValue(operand.name) ?? fallback : fallback;
}

function formatDecisionValue(value: unknown): string {
  if (value == null) return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(formatDecisionValue).join(", ");
  const record = recordValue(value);
  if (!record) return String(value);
  if ("value" in record) {
    const unit = stringValue(record.unit);
    return `${formatDecisionValue(record.value)}${unit ? ` ${unit}` : ""}`;
  }
  return Object.entries(record).map(([key, nested]) => `${formatToken(key)}: ${formatDecisionValue(nested)}`).join("; ");
}

function recordValue(value: unknown): BacktestDecisionDetails | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as BacktestDecisionDetails
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EquityTooltip({
  active,
  label,
  payload
}: {
  active?: boolean;
  label?: string | number;
  payload?: Array<{ dataKey?: string; value?: number | string | null }>;
}) {
  if (!active || label == null || !payload?.length) {
    return null;
  }
  const items = payload
    .filter((entry) => entry.dataKey && entry.dataKey !== "timestampMs" && entry.value != null)
    .map((entry) => ({
      label: String(entry.dataKey) === "ideal" ? "Ideal" : "Actual",
      value: formatCurrency(Number(entry.value))
    }));
  if (!items.length) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <strong>{formatDate(new Date(Number(label)).toISOString())}</strong>
      {items.map((item) => (
        <div key={item.label}>
          {item.label}: {item.value}
        </div>
      ))}
    </div>
  );
}

function buildEquityChartData(equity?: BacktestEquityResponse) {
  if (!equity) {
    return [];
  }
  const byTimestamp = new Map<number, { timestampMs: number; ideal?: number; actual?: number }>();
  for (const series of equity.series) {
    for (const point of series.points) {
      const timestampMs = new Date(point.as_of).getTime();
      if (Number.isNaN(timestampMs) || point.equity == null) {
        continue;
      }
      const row = byTimestamp.get(timestampMs) ?? { timestampMs };
      if (series.timing_scenario === "ideal") {
        row.ideal = point.equity;
      } else {
        row.actual = point.equity;
      }
      byTimestamp.set(timestampMs, row);
    }
  }
  return Array.from(byTimestamp.values()).sort((left, right) => left.timestampMs - right.timestampMs);
}

function statusChipClass(status: string) {
  if (status === "failed") {
    return "chip warning";
  }
  return "chip";
}

// Fraction of the run window analyzed before a token-budget exhaustion stopped it,
// derived from the coverage boundary and the window bounds. Null when coverage is
// complete or the boundary is unknown.
function windowCoverageFraction(run: BacktestRunSummary): number | null {
  if (!run.analysis_coverage_until_at) {
    return null;
  }
  const start = new Date(run.window_start_at).getTime();
  const end = new Date(run.window_end_at).getTime();
  const covered = new Date(run.analysis_coverage_until_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || Number.isNaN(covered) || end <= start) {
    return null;
  }
  return Math.max(0, Math.min(1, (covered - start) / (end - start)));
}

// One-line note describing a partial-coverage run, shown in the runs table tooltip,
// the window subtext, and the Run Summary header.
function budgetExhaustionNote(run: BacktestRunSummary): string {
  const through = run.analysis_coverage_until_at
    ? ` — analyzed through ${formatDate(run.analysis_coverage_until_at)}`
    : "";
  const fraction = windowCoverageFraction(run);
  const pct = fraction == null ? "" : `, ${Math.round(fraction * 100)}% of window`;
  return `token budget exhausted${through}${pct}`;
}

function formatInteger(value?: number | null) {
  return value == null ? "—" : value.toLocaleString();
}

function formatNumber(value?: number | null) {
  return value == null ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatCurrency(value?: number | null) {
  if (value == null) {
    return "—";
  }
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2
  });
}

function formatPercent(value?: number | null) {
  if (value == null) {
    return "—";
  }
  return `${(value * 100).toFixed(2)}%`;
}

function formatDuration(seconds?: number | null) {
  if (seconds == null) {
    return "—";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function formatDate(value?: string | null) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function formatToken(value: string) {
  return value.replaceAll("_", " ");
}

function localDateToIso(value: string, boundary: "start" | "end") {
  // date has no timezone; the operator enters UTC, so apply the requested day boundary.
  return `${value}T${boundary === "start" ? "00:00:00" : "23:59:59"}Z`;
}
