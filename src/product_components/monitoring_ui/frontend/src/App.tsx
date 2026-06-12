import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  fetchBacklog,
  fetchDeadLetters,
  fetchFilterQualityIncorrectlyRejected,
  fetchHealth,
  fetchProductionFilterConfig,
  fetchFilterQualityStatus,
  fetchProviders,
  fetchTestFilterConfig,
  fetchThroughput,
  promoteTestFilterConfig,
  runTestFilterSimulation,
  saveTestFilterConfig,
  startFilterQualityRun,
  type FilterQualityIncorrectlyRejectedItem,
  type FilterQualityRunSummary,
  type HealthState,
  type NewsFilterConfigPayload
} from "./api";

export function App() {
  const queryClient = useQueryClient();
  const [incorrectlyRejectedOpen, setIncorrectlyRejectedOpen] = useState(false);
  const [evaluationStartRequested, setEvaluationStartRequested] = useState(false);
  const [requestedEvaluationAction, setRequestedEvaluationAction] = useState<"filter-quality" | "simulation" | null>(null);
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth });
  const providers = useQuery({ queryKey: ["providers"], queryFn: fetchProviders, refetchInterval: 10000 });
  const throughput = useQuery({ queryKey: ["throughput", "1h"], queryFn: () => fetchThroughput("1h") });
  const backlog = useQuery({ queryKey: ["backlog"], queryFn: fetchBacklog, refetchInterval: 20000 });
  const deadLetters = useQuery({ queryKey: ["dead-letter"], queryFn: fetchDeadLetters, refetchInterval: 20000 });
  const filterQuality = useQuery({
    queryKey: ["filter-quality"],
    queryFn: fetchFilterQualityStatus,
    refetchInterval: 10000
  });
  const lastFilterQualityRunId = filterQuality.data?.last_run?.run_id;
  const incorrectlyRejected = useQuery({
    queryKey: ["filter-quality", lastFilterQualityRunId, "incorrectly-rejected"],
    queryFn: () => fetchFilterQualityIncorrectlyRejected(lastFilterQualityRunId ?? ""),
    enabled: incorrectlyRejectedOpen && Boolean(lastFilterQualityRunId)
  });
  const productionFilter = useQuery({
    queryKey: ["filter-config", "production"],
    queryFn: fetchProductionFilterConfig
  });
  const testFilter = useQuery({
    queryKey: ["filter-config", "test"],
    queryFn: fetchTestFilterConfig
  });
  const saveTestFilter = useMutation({
    mutationFn: saveTestFilterConfig,
    onSuccess: (config) => {
      queryClient.setQueryData(["filter-config", "test"], config);
      queryClient.invalidateQueries({ queryKey: ["filter-config", "test"] });
    }
  });
  const runSimulation = useMutation({
    mutationFn: runTestFilterSimulation,
    onMutate: () => {
      setEvaluationStartRequested(true);
      setRequestedEvaluationAction("simulation");
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["filter-quality"] }),
    onError: () => {
      setEvaluationStartRequested(false);
      setRequestedEvaluationAction(null);
    }
  });
  const promoteFilter = useMutation({
    mutationFn: promoteTestFilterConfig,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["filter-config"] });
      queryClient.invalidateQueries({ queryKey: ["filter-quality"] });
    }
  });
  const startFilterQuality = useMutation({
    mutationFn: startFilterQualityRun,
    onMutate: () => {
      setEvaluationStartRequested(true);
      setRequestedEvaluationAction("filter-quality");
    },
    onSuccess: () => {
      setIncorrectlyRejectedOpen(false);
      queryClient.invalidateQueries({ queryKey: ["filter-quality"] });
    },
    onError: () => {
      setEvaluationStartRequested(false);
      setRequestedEvaluationAction(null);
    }
  });
  useEffect(() => {
    if (filterQuality.data?.running_run) {
      setEvaluationStartRequested(false);
      setRequestedEvaluationAction(null);
    }
  }, [filterQuality.data?.running_run]);

  const evaluationRunningOrStarting =
    Boolean(filterQuality.data?.running_run) ||
    evaluationStartRequested ||
    startFilterQuality.isPending ||
    runSimulation.isPending;

  const chartRows =
    throughput.data?.buckets.map((bucket) => ({
      time: new Date(bucket.window_start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      fetched: bucket.fetch_count,
      published: bucket.publish_success_count,
      failed: bucket.publish_error_count
    })) ?? [];

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Trader</p>
          <h1>Monitoring UI</h1>
        </div>
        <div className="timestamp">
          Last refresh
          <strong>{formatDate(health.data?.last_successful_refresh_at)}</strong>
        </div>
      </header>

      <section className="status-grid" aria-label="Global health">
        <StatusTile label="Readiness" state={health.data?.readiness} loading={health.isLoading} />
        <StatusTile label="Liveness" state={health.data?.liveness} loading={health.isLoading} />
        <MetricTile label="Incidents" value={health.data?.active_incident_count ?? 0} />
        <MetricTile label="Dead letters" value={backlog.data?.dead_letter_count ?? 0} />
      </section>

      <section className="layout">
        <div className="panel panel-large">
          <div className="panel-heading">
            <h2>Throughput</h2>
            <span>1h window</span>
          </div>
          <div className="chart">
            {chartRows.length > 0 ? (
              <ResponsiveContainer width="100%" height={290} minWidth={0}>
                <AreaChart data={chartRows}>
                  <defs>
                    <linearGradient id="published" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#1d8f6f" stopOpacity={0.35} />
                      <stop offset="95%" stopColor="#1d8f6f" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#dbe3dc" strokeDasharray="3 3" />
                  <XAxis dataKey="time" tickLine={false} axisLine={false} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                  <Tooltip />
                  <Area type="monotone" dataKey="published" stroke="#1d8f6f" fill="url(#published)" />
                  <Area type="monotone" dataKey="failed" stroke="#b83b3b" fill="#b83b3b22" />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState text={throughput.isError ? "Throughput data unavailable" : "No throughput data yet"} />
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-heading">
            <h2>Dependencies</h2>
          </div>
          <div className="dependency-list">
            {(health.data?.dependencies ?? []).map((dependency) => (
              <div className="dependency" key={dependency.name}>
                <span className={`dot ${dependency.state}`} />
                <div>
                  <strong>{dependency.name}</strong>
                  <span>{dependency.message ?? dependency.kind}</span>
                </div>
              </div>
            ))}
            {!health.data?.dependencies.length && <EmptyState text="Dependency state unavailable" />}
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Providers</h2>
          <span>{providers.data?.providers.length ?? 0} configured</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Last cycle</th>
                <th>Fetched</th>
                <th>Published</th>
                <th>Errors</th>
              </tr>
            </thead>
            <tbody>
              {(providers.data?.providers ?? []).map((provider) => (
                <tr key={provider.source_key}>
                  <td>{provider.source_key}</td>
                  <td>{formatDate(provider.last_cycle_end_at)}</td>
                  <td>{provider.fetch_count}</td>
                  <td>{provider.publish_success_count}</td>
                  <td>{provider.last_error_code ?? provider.fetch_error_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!providers.data?.providers.length && <EmptyState text="No provider telemetry yet" />}
        </div>
      </section>

      <section className="layout">
        <div className="panel">
          <div className="panel-heading">
            <h2>Backlog</h2>
          </div>
          <div className="backlog-grid">
            <MetricTile label="Pending" value={backlog.data?.pending_count ?? 0} compact />
            <MetricTile label="Retrying" value={backlog.data?.retrying_count ?? 0} compact />
            <MetricTile label="Max age" value={formatDuration(backlog.data?.max_attempt_age_seconds)} compact />
          </div>
        </div>

        <div className="panel">
          <div className="panel-heading">
            <h2>Dead Letter</h2>
          </div>
          <div className="dead-letter-list">
            {(deadLetters.data?.items ?? []).map((item) => (
              <div className="dead-letter" key={item.obligation_id}>
                <strong>{item.source_key}</strong>
                <span>{item.reason ?? "unknown_error"}</span>
                <small>{formatDate(item.updated_at)}</small>
              </div>
            ))}
            {!deadLetters.data?.items.length && <EmptyState text="No dead-lettered items" />}
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div className="heading-with-state">
            <h2>Filter Quality</h2>
            <span className={`state-pill ${evaluationStartRequested ? "starting" : filterQualityState(filterQuality.data?.running_run, filterQuality.data?.last_run)}`}>
              {evaluationStartRequested ? "starting" : filterQualityState(filterQuality.data?.running_run, filterQuality.data?.last_run)}
            </span>
            {filterQuality.data?.last_run && (
              <span className="state-pill subject-pill">
                {formatEvaluationSubject(filterQuality.data.last_run.evaluation_subject)}
              </span>
            )}
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={evaluationRunningOrStarting}
            onClick={() => startFilterQuality.mutate()}
          >
            {startFilterQuality.isPending || requestedEvaluationAction === "filter-quality" ? "Starting" : "Run filter quality"}
          </button>
        </div>
        {startFilterQuality.isError && (
          <div className="inline-error">{startFilterQuality.error.message}</div>
        )}
        {filterQuality.data?.last_run ? (
          <FilterQualityMetrics
            run={filterQuality.data.last_run}
            incorrectlyRejectedOpen={incorrectlyRejectedOpen}
            incorrectlyRejectedItems={incorrectlyRejected.data?.items ?? []}
            incorrectlyRejectedLoading={!incorrectlyRejected.data && incorrectlyRejected.isFetching}
            incorrectlyRejectedError={incorrectlyRejected.isError}
            testFilter={testFilter.data}
            productionFilter={productionFilter.data}
            saveTestFilter={(config) => saveTestFilter.mutate(config)}
            savePending={saveTestFilter.isPending}
            runSimulation={() => runSimulation.mutate()}
            runSimulationPending={runSimulation.isPending}
            simulationStartRequested={requestedEvaluationAction === "simulation"}
            evaluationRunningOrStarting={evaluationRunningOrStarting}
            promoteFilter={() => promoteFilter.mutate()}
            promotePending={promoteFilter.isPending}
            onToggleIncorrectlyRejected={() => setIncorrectlyRejectedOpen((open) => !open)}
          />
        ) : (
          <EmptyState text={filterQuality.isError ? "Filter quality data unavailable" : "No filter quality runs yet"} />
        )}
      </section>
    </main>
  );
}

function StatusTile({ label, state, loading }: { label: string; state?: HealthState; loading?: boolean }) {
  const display = loading ? "loading" : state ?? "unknown";
  return (
    <div className="status-tile">
      <span>{label}</span>
      <strong className={state ?? ""}>{display}</strong>
    </div>
  );
}

function MetricTile({ label, value, compact = false }: { label: string; value: string | number; compact?: boolean }) {
  return (
    <div className={compact ? "metric compact" : "metric"}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function FilterQualityMetrics({
  run,
  incorrectlyRejectedOpen,
  incorrectlyRejectedItems,
  incorrectlyRejectedLoading,
  incorrectlyRejectedError,
  testFilter,
  productionFilter,
  saveTestFilter,
  savePending,
  runSimulation,
  runSimulationPending,
  simulationStartRequested,
  evaluationRunningOrStarting,
  promoteFilter,
  promotePending,
  onToggleIncorrectlyRejected
}: {
  run: FilterQualityRunSummary;
  incorrectlyRejectedOpen: boolean;
  incorrectlyRejectedItems: FilterQualityIncorrectlyRejectedItem[];
  incorrectlyRejectedLoading: boolean;
  incorrectlyRejectedError: boolean;
  testFilter?: NewsFilterConfigPayload;
  productionFilter?: NewsFilterConfigPayload;
  saveTestFilter: (config: NewsFilterConfigPayload) => void;
  savePending: boolean;
  runSimulation: () => void;
  runSimulationPending: boolean;
  simulationStartRequested: boolean;
  evaluationRunningOrStarting: boolean;
  promoteFilter: () => void;
  promotePending: boolean;
  onToggleIncorrectlyRejected: () => void;
}) {
  return (
    <div className="quality-section">
      <div className="quality-grid">
        <QualityValue label="Total quality" value={formatRate(run.total_filter_quality)} />
        <QualityValue label="Rejection precision" value={formatRate(run.rejection_precision_proxy)} />
        <QualityValue label="Incorrectly accepted" value={formatRate(run.incorrectly_accepted_rate_estimate)} />
        <QualityValue
          label="Incorrectly rejected"
          value={run.incorrectly_rejected_count}
          buttonDisabled={run.incorrectly_rejected_count === 0}
          onClick={run.incorrectly_rejected_count > 0 ? onToggleIncorrectlyRejected : undefined}
        />
        <QualityValue label="Rejected evaluated" value={run.rejected_items_evaluated} />
        <QualityValue label="Accepted sampled" value={run.accepted_items_sampled} />
        <QualityValue label="Accepted assumed correct" value={run.assumed_correct_accepted_count} />
        <QualityValue label="Item failures" value={run.item_failed_count} />
        <QualityValue label="Dataset input" value={run.dataset_input_count} />
        <QualityValue label="Last finished" value={formatDate(run.finished_at)} />
        {run.item_failed_count > 0 && <QualityValue label="Failure reason" value={formatErrorCodes(run.item_error_codes)} />}
        {run.status === "failed" && <QualityValue label="Error" value={run.error_code ?? "unknown_error"} />}
      </div>
      {incorrectlyRejectedOpen && (
        <IncorrectlyRejectedTable
          items={incorrectlyRejectedItems}
          lastRun={run}
          loading={incorrectlyRejectedLoading}
          error={incorrectlyRejectedError}
          testFilter={testFilter}
          productionFilter={productionFilter}
          saveTestFilter={saveTestFilter}
          savePending={savePending}
          runSimulation={runSimulation}
          runSimulationPending={runSimulationPending}
          simulationStartRequested={simulationStartRequested}
          evaluationRunningOrStarting={evaluationRunningOrStarting}
          promoteFilter={promoteFilter}
          promotePending={promotePending}
        />
      )}
    </div>
  );
}

function QualityValue({
  label,
  value,
  buttonDisabled = false,
  onClick
}: {
  label: string;
  value: string | number;
  buttonDisabled?: boolean;
  onClick?: () => void;
}) {
  return (
    <div className="quality-value">
      <span>{label}</span>
      {onClick ? (
        <button type="button" className="quality-link" disabled={buttonDisabled} onClick={onClick}>
          {value}
        </button>
      ) : (
        <strong>{value}</strong>
      )}
    </div>
  );
}

function IncorrectlyRejectedTable({
  items,
  lastRun,
  loading,
  error,
  testFilter,
  productionFilter,
  saveTestFilter,
  savePending,
  runSimulation,
  runSimulationPending,
  simulationStartRequested,
  evaluationRunningOrStarting,
  promoteFilter,
  promotePending
}: {
  items: FilterQualityIncorrectlyRejectedItem[];
  lastRun: FilterQualityRunSummary;
  loading: boolean;
  error: boolean;
  testFilter?: NewsFilterConfigPayload;
  productionFilter?: NewsFilterConfigPayload;
  saveTestFilter: (config: NewsFilterConfigPayload) => void;
  savePending: boolean;
  runSimulation: () => void;
  runSimulationPending: boolean;
  simulationStartRequested: boolean;
  evaluationRunningOrStarting: boolean;
  promoteFilter: () => void;
  promotePending: boolean;
}) {
  const [selectedKeywords, setSelectedKeywords] = useState<Set<string>>(new Set());
  const mergedKeywords = mergeRecommendedKeywords(items);
  const selectedKeywordList = normalizeList([...selectedKeywords]);
  const activeTestFilter = displayedTestFilterDraft({
    lastRun,
    testFilter,
    productionFilter
  });

  if (loading) {
    return <EmptyState text="Loading incorrectly rejected records" />;
  }
  if (error) {
    return <EmptyState text="Incorrectly rejected records unavailable" />;
  }
  if (!items.length) {
    return <EmptyState text="No incorrectly rejected records" />;
  }
  const updateSelected = (keyword: string, checked: boolean) => {
    setSelectedKeywords((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(keyword);
      } else {
        next.delete(keyword);
      }
      return next;
    });
  };
  const selectAll = () => {
    setSelectedKeywords(new Set(mergedKeywords.map((item) => item.keyword)));
  };
  const saveSelectedToTestFilter = () => {
    if (!activeTestFilter) {
      return;
    }
    const include = normalizeList([...activeTestFilter.include_keywords, ...selectedKeywordList]);
    saveTestFilter({ ...activeTestFilter, config_role: "test", status: "active", include_keywords: include });
    setSelectedKeywords(new Set());
  };
  return (
    <div className="quality-details">
      <div className="keyword-actions">
        <div>
          <strong>Recommended include keywords</strong>
          <div className="keyword-list">
            {mergedKeywords.map((item) => (
              <label className="keyword-chip" key={item.keyword}>
                <input
                  type="checkbox"
                  checked={selectedKeywords.has(item.keyword)}
                  onChange={(event) => updateSelected(item.keyword, event.target.checked)}
                />
                {item.keyword} ({item.count})
              </label>
            ))}
            {!mergedKeywords.length && <span className="muted">No structured keyword recommendations</span>}
          </div>
        </div>
        <div className="keyword-entry">
          <button type="button" className="secondary-button" onClick={selectAll} disabled={!mergedKeywords.length}>
            Select all
          </button>
          <button type="button" className="primary-button" onClick={saveSelectedToTestFilter} disabled={!selectedKeywordList.length || !activeTestFilter || savePending}>
            {savePending ? "Saving" : "Add to test filter"}
          </button>
        </div>
        {activeTestFilter && (
          <FilterConfigEditor
            config={activeTestFilter}
            saveTestFilter={saveTestFilter}
            savePending={savePending}
            runSimulation={runSimulation}
            runSimulationPending={runSimulationPending}
            simulationStartRequested={simulationStartRequested}
            evaluationRunningOrStarting={evaluationRunningOrStarting}
            promoteFilter={promoteFilter}
            promotePending={promotePending}
          />
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Article</th>
              <th>Keywords</th>
              <th>Published</th>
              <th>Source</th>
              <th>Rejection reason</th>
              <th>Cause</th>
              <th>Solution</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.assessment_id}>
                <td className="article-cell">
                  <a href={item.url} target="_blank" rel="noreferrer">
                    {item.headline}
                  </a>
                  {displayedMatchedArticle(item, lastRun).url && displayedMatchedArticle(item, lastRun).headline && (
                    <small>
                      Duplicate of{" "}
                      <a href={displayedMatchedArticle(item, lastRun).url ?? undefined} target="_blank" rel="noreferrer">
                        {displayedMatchedArticle(item, lastRun).headline}
                      </a>
                      {displayedMatchedArticle(item, lastRun).source
                        ? ` (${displayedMatchedArticle(item, lastRun).source})`
                        : ""}
                      {displayedMatchedArticle(item, lastRun).published_at
                        ? `, ${formatDate(displayedMatchedArticle(item, lastRun).published_at)}`
                        : ""}
                    </small>
                  )}
                  {item.rationale && <small>{item.rationale}</small>}
                </td>
                <td className="keyword-cell">
                  {item.recommended_include_keywords.map((keyword) => (
                    <label className="keyword-chip compact-chip" key={`${item.assessment_id}-${keyword}`}>
                      <input
                        type="checkbox"
                        checked={selectedKeywords.has(keyword)}
                        onChange={(event) => updateSelected(keyword, event.target.checked)}
                      />
                      {keyword}
                    </label>
                  ))}
                  {!item.recommended_include_keywords.length && "n/a"}
                </td>
                <td>{formatDate(item.published_at)}</td>
                <td>{item.source}</td>
                <td>{displayedRejectionReason(item, lastRun) ?? "n/a"}</td>
                <td>{formatCause(displayedCause(item, lastRun))}</td>
                <td className="solution-cell">{displayedSolution(item, lastRun) ?? "n/a"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FilterConfigEditor({
  config,
  saveTestFilter,
  savePending,
  runSimulation,
  runSimulationPending,
  simulationStartRequested,
  evaluationRunningOrStarting,
  promoteFilter,
  promotePending
}: {
  config: NewsFilterConfigPayload;
  saveTestFilter: (config: NewsFilterConfigPayload) => void;
  savePending: boolean;
  runSimulation: () => void;
  runSimulationPending: boolean;
  simulationStartRequested: boolean;
  evaluationRunningOrStarting: boolean;
  promoteFilter: () => void;
  promotePending: boolean;
}) {
  const [draft, setDraft] = useState(config);
  useEffect(() => {
    setDraft(config);
  }, [config]);
  const updateList = (key: "include_keywords" | "exclude_keywords" | "watchlist_tickers", value: string) => {
    setDraft({ ...draft, [key]: normalizeList(value.split(",")) });
  };
  return (
    <div className="filter-config-group">
      <div className="filter-config-heading">
        <strong>Filter config</strong>
        <span>Test filter</span>
      </div>
      <div className="filter-editor">
        <label>
          Include keywords
          <textarea value={draft.include_keywords.join(", ")} onChange={(event) => updateList("include_keywords", event.target.value)} />
        </label>
        <label>
          Exclude keywords
          <textarea value={draft.exclude_keywords.join(", ")} onChange={(event) => updateList("exclude_keywords", event.target.value)} />
        </label>
        <label>
          Watchlist tickers
          <textarea value={draft.watchlist_tickers.join(", ")} onChange={(event) => updateList("watchlist_tickers", event.target.value.toUpperCase())} />
        </label>
        <div className="filter-editor-row">
          <label>
            Dedupe algorithm
            <input value={draft.dedupe_algorithm} onChange={(event) => setDraft({ ...draft, dedupe_algorithm: event.target.value })} />
          </label>
          <label>
            Threshold
            <input type="number" min="0" max="1" step="0.01" value={draft.dedupe_similarity_threshold} onChange={(event) => setDraft({ ...draft, dedupe_similarity_threshold: Number(event.target.value) })} />
          </label>
          <label>
            Lookback hours
            <input type="number" min="1" step="1" value={draft.dedupe_lookback_hours} onChange={(event) => setDraft({ ...draft, dedupe_lookback_hours: Number(event.target.value) })} />
          </label>
        </div>
        <div className="filter-editor-actions">
          <button type="button" className="primary-button" onClick={() => saveTestFilter({ ...draft, config_role: "test", status: "active" })} disabled={savePending}>
            {savePending ? "Saving" : "Save test filter"}
          </button>
          <button type="button" className="secondary-button" onClick={runSimulation} disabled={evaluationRunningOrStarting}>
            {runSimulationPending || simulationStartRequested ? "Running" : "Run simulation"}
          </button>
          <button type="button" className="secondary-button" onClick={promoteFilter} disabled={promotePending}>
            {promotePending ? "Saving" : "Save test filter as production"}
          </button>
        </div>
      </div>
    </div>
  );
}

function formatDate(value?: string | null) {
  if (!value) {
    return "n/a";
  }
  return new Date(value).toLocaleString();
}

function formatDuration(seconds?: number | null) {
  if (seconds == null) {
    return "n/a";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  return `${Math.round(seconds / 60)}m`;
}

function formatRate(value?: number | null) {
  if (value == null) {
    return "n/a";
  }
  return `${(value * 100).toFixed(2)}%`;
}

function filterQualityState(running?: FilterQualityRunSummary | null, last?: FilterQualityRunSummary | null) {
  if (running) {
    return "running";
  }
  return last?.status ?? "no runs";
}

function formatEvaluationSubject(value?: string | null) {
  if (value === "simulation") {
    return "Evaluation: Simulation";
  }
  if (value === "production") {
    return "Evaluation: Production";
  }
  return "Evaluation: Unknown";
}

function displayedTestFilterDraft({
  lastRun,
  testFilter,
  productionFilter
}: {
  lastRun: FilterQualityRunSummary;
  testFilter?: NewsFilterConfigPayload;
  productionFilter?: NewsFilterConfigPayload;
}) {
  if (testFilter?.created_from_run_id === lastRun.run_id) {
    return testFilter;
  }
  if (
    testFilter &&
    lastRun.evaluated_filter_config &&
    !sameList(testFilter.include_keywords, lastRun.evaluated_filter_config.include_keywords)
  ) {
    return testFilter;
  }
  if (lastRun.evaluated_filter_config) {
    return testDraftFromSource(lastRun.evaluated_filter_config, testFilter, lastRun.run_id);
  }
  if (lastRun.evaluation_subject === "production" && productionFilter) {
    return testDraftFromSource(productionFilter, testFilter, lastRun.run_id);
  }
  if (testFilter) {
    return testFilter;
  }
  return productionFilter ? testDraftFromSource(productionFilter, undefined, undefined) : undefined;
}

function testDraftFromSource(
  sourceFilter: NewsFilterConfigPayload,
  testFilter: NewsFilterConfigPayload | undefined,
  createdFromRunId: string | undefined
): NewsFilterConfigPayload {
  return {
    ...sourceFilter,
    filter_config_id: testFilter?.filter_config_id,
    config_name: testFilter?.config_name ?? "Test filter",
    config_role: "test",
    status: "active",
    include_keywords: [...sourceFilter.include_keywords],
    exclude_keywords: [...sourceFilter.exclude_keywords],
    watchlist_tickers: [...sourceFilter.watchlist_tickers],
    created_from_run_id: createdFromRunId
  };
}

function displayedRejectionReason(item: FilterQualityIncorrectlyRejectedItem, run: FilterQualityRunSummary) {
  if (run.evaluation_subject === "production") {
    return item.production_rejection_reason_code ?? item.rejection_reason_code;
  }
  if (run.evaluation_subject === "simulation") {
    return item.simulation_rejection_reason_code ?? item.rejection_reason_code;
  }
  return item.rejection_reason_code;
}

function displayedMatchedArticle(item: FilterQualityIncorrectlyRejectedItem, run: FilterQualityRunSummary) {
  if (run.evaluation_subject === "production") {
    return {
      id: item.production_matched_article_id,
      headline: item.production_matched_article_headline,
      url: item.production_matched_article_url,
      source: item.production_matched_article_source,
      published_at: item.production_matched_article_published_at
    };
  }
  if (run.evaluation_subject === "simulation") {
    return {
      id: item.simulation_matched_article_id,
      headline: item.simulation_matched_article_headline,
      url: item.simulation_matched_article_url,
      source: item.simulation_matched_article_source,
      published_at: item.simulation_matched_article_published_at
    };
  }
  return {
    id: item.production_matched_article_id ?? item.simulation_matched_article_id,
    headline: item.production_matched_article_headline ?? item.simulation_matched_article_headline,
    url: item.production_matched_article_url ?? item.simulation_matched_article_url,
    source: item.production_matched_article_source ?? item.simulation_matched_article_source,
    published_at:
      item.production_matched_article_published_at ?? item.simulation_matched_article_published_at
  };
}

function displayedCause(item: FilterQualityIncorrectlyRejectedItem, run: FilterQualityRunSummary) {
  const reason = displayedRejectionReason(item, run);
  if (reason === "rejected_soft_duplicate" || reason === "rejected_strong_duplicate") {
    return "dedupe_threshold_issue";
  }
  if (reason === "rejected_not_relevant") {
    return item.probable_cause;
  }
  if (reason === "rejected_excluded_keyword") {
    return "rule_conflict";
  }
  return item.probable_cause;
}

function displayedSolution(item: FilterQualityIncorrectlyRejectedItem, run: FilterQualityRunSummary) {
  const reason = displayedRejectionReason(item, run);
  if (reason === "rejected_soft_duplicate" || reason === "rejected_strong_duplicate") {
    return "Review dedupe scoring or threshold for this article pair before changing include keywords.";
  }
  if (reason === "rejected_excluded_keyword") {
    return "Review the matching exclude keyword before changing include keywords.";
  }
  return item.improvement_suggestion;
}

function formatErrorCodes(codes: Record<string, number>) {
  const entries = Object.entries(codes);
  if (!entries.length) {
    return "n/a";
  }
  return entries
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([code, count]) => `${code} (${count})`)
    .join(", ");
}

function formatCause(value?: string | null) {
  if (!value) {
    return "n/a";
  }
  return value.replaceAll("_", " ");
}

function mergeRecommendedKeywords(items: FilterQualityIncorrectlyRejectedItem[]) {
  const counts = new Map<string, number>();
  for (const item of items) {
    for (const keyword of item.recommended_include_keywords) {
      const normalized = keyword.trim().toLowerCase();
      if (!normalized) {
        continue;
      }
      counts.set(normalized, (counts.get(normalized) ?? 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .map(([keyword, count]) => ({ keyword, count }))
    .sort((left, right) => right.count - left.count || left.keyword.localeCompare(right.keyword));
}

function normalizeList(values: string[]) {
  return Array.from(
    new Set(values.map((value) => value.trim().toLowerCase()).filter(Boolean))
  ).sort();
}

function sameList(left: string[], right: string[]) {
  const normalizedLeft = normalizeList(left);
  const normalizedRight = normalizeList(right);
  return (
    normalizedLeft.length === normalizedRight.length &&
    normalizedLeft.every((value, index) => value === normalizedRight[index])
  );
}
