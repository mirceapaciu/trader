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
  type BacktestCardStatusBucket,
  type BacktestCardsResponse,
  type BacktestDelays,
  type BacktestDetailResponse,
  type BacktestEquityResponse,
  type BacktestMetrics,
  type BacktestRegenerationStats,
  type BacktestRunSummary,
  type BacktestStrategyMetrics,
  type BacktestTradeFilters,
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
      window_start_at: localDateTimeToIso(windowStart),
      window_end_at: localDateTimeToIso(windowEnd),
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
        <div className="filter-editor-row">
          <label>
            Window start (UTC)
            <input
              type="datetime-local"
              value={windowStart}
              onChange={(event) => setWindowStart(event.target.value)}
              disabled={busy}
            />
          </label>
          <label>
            Window end (UTC)
            <input
              type="datetime-local"
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
              Set a full UTC date <em>and</em> time for both window start and end to enable this.
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
                  </td>
                  <td>
                    {formatDate(run.window_start_at)}
                    <span className="table-subtext">to {formatDate(run.window_end_at)}</span>
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
      <TradesPanel runId={runId} />
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
          </span>
        </div>
      </div>
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

function RegenerationPanel({ stats }: { stats: BacktestRegenerationStats }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Regeneration</h2>
          <span>
            Article analysis funnel for this run
            {stats.budget_exhausted ? " · token budget exhausted (window not fully covered)" : ""}
          </span>
        </div>
      </div>
      <div className="thesis-kpi-grid">
        <MetricTile label="Articles in window" value={formatInteger(stats.articles_found)} />
        <MetricTile label="Articles relevant" value={formatInteger(stats.articles_relevant)} />
        <MetricTile label="Articles analyzed" value={formatInteger(stats.articles_analyzed)} />
        <MetricTile label="Analyses (LLM calls)" value={formatInteger(stats.analyses_created)} />
        <MetricTile label="Evidence windows" value={formatInteger(stats.evidence_windows_created)} />
        <MetricTile label="Cards created" value={formatInteger(stats.cards_created)} />
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
                <th>Risk blocked</th>
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
                <th>Risk blocked</th>
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
  const selectedCard = rows.find((card) => card.thesis_card_id === selectedId) ?? null;

  // Reset the selection when switching runs so we never show a stale card.
  useEffect(() => {
    setSelectedId(null);
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
                  <th>Trades</th>
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
              <CardDetail card={selectedCard} />
            ) : (
              <div className="empty">Select a card to see details.</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function CardDetail({ card }: { card: BacktestCard }) {
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
        <span>Trades</span>
        <strong>{card.trades.length} trade{card.trades.length === 1 ? "" : "s"}</strong>
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
                <th>Exit reason</th>
              </tr>
            </thead>
            <tbody>
              {card.trades.map((trade) => (
                <tr key={trade.trade_id}>
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
                  <td>{trade.exit_reason ? formatToken(trade.exit_reason) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TradesPanel({ runId }: { runId: string }) {
  const [filters, setFilters] = useState<{
    timing_scenario: string;
    strategy: string;
    exit_reason: string;
    card_status: string;
  }>({ timing_scenario: "", strategy: "", exit_reason: "", card_status: "" });
  const [offset, setOffset] = useState(0);

  // Reset pagination when the run or filters change.
  useEffect(() => {
    setOffset(0);
  }, [runId, filters]);

  const queryFilters: BacktestTradeFilters = {
    timing_scenario: filters.timing_scenario || undefined,
    strategy: filters.strategy || undefined,
    exit_reason: filters.exit_reason || undefined,
    card_status: filters.card_status || undefined,
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

  const updateFilter = (key: keyof typeof filters, value: string) =>
    setFilters((current) => ({ ...current, [key]: value }));

  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Trades</h2>
          <span>{totalCount} trade{totalCount === 1 ? "" : "s"}</span>
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
          Exit reason
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
                <th>Exit reason</th>
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
                  <td>{trade.exit_reason ? formatToken(trade.exit_reason) : "—"}</td>
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
    </section>
  );
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

function localDateTimeToIso(value: string) {
  // datetime-local has no timezone; the operator enters UTC, so append Z.
  return `${value}:00Z`;
}
