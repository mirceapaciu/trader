import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";
import {
  ApiError,
  fetchBacklog,
  fetchDeadLetters,
  fetchFetchedArticles,
  fetchFilterQualityIncorrectlyAccepted,
  fetchFilterQualityIncorrectlyRejected,
  fetchHealth,
  fetchProductionFilterConfig,
  fetchFilterQualityStatus,
  fetchProviders,
  fetchTestFilterConfig,
  fetchThroughput,
  promoteTestFilterConfig,
  reprocessNewsFetcherRejected,
  runTestFilterSimulation,
  saveTestFilterConfig,
  startFilterQualityRun,
  type FetchedArticle,
  type FilterQualityIncorrectlyAcceptedItem,
  type FilterQualityIncorrectlyRejectedItem,
  type FilterQualityRunSummary,
  type HealthState,
  type NewsFetcherReprocessRejectedResponse,
  type NewsFilterConfigPayload,
  type ProviderStatus,
  type ThroughputPresetWindow,
  type ThroughputRequest,
  type ThroughputResponse
} from "../api";

const THROUGHPUT_PRESETS: Array<{ label: string; window: ThroughputPresetWindow }> = [
  { label: "15m", window: "15m" },
  { label: "1h", window: "1h" },
  { label: "1d", window: "1d" },
  { label: "7d", window: "7d" },
  { label: "30d", window: "30d" }
];

type ThroughputSelection = {
  window: ThroughputPresetWindow;
  endDate: string;
};

const PROVIDER_GROUPS: Array<{ key: string; pattern: RegExp }> = [
  { key: "rss:yahoo_finance:batch", pattern: /^rss:yahoo_finance:batch:/ },
  { key: "finnhub:company", pattern: /^finnhub:company:/ }
];

export function NewsFetcherTab() {
  const queryClient = useQueryClient();
  const [qualityDetailView, setQualityDetailView] = useState<"incorrectly-rejected" | "incorrectly-accepted" | null>(null);
  const [acceptedAuditEnabled, setAcceptedAuditEnabled] = useState(false);
  const [evaluationStartRequested, setEvaluationStartRequested] = useState(false);
  const [requestedEvaluationAction, setRequestedEvaluationAction] = useState<"filter-quality" | "simulation" | null>(null);
  const [throughputSelection, setThroughputSelection] = useState<ThroughputSelection>({
    window: "1d",
    endDate: ""
  });
  const [fetchedWindow, setFetchedWindow] = useState<ThroughputPresetWindow>("1d");
  const [reprocessResult, setReprocessResult] = useState<NewsFetcherReprocessRejectedResponse | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(
    new Set(PROVIDER_GROUPS.map(group => group.key))
  );
  function toggleProviderGroup(group: string) {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group); else next.add(group);
      return next;
    });
  }
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth });
  const providers = useQuery({ queryKey: ["providers"], queryFn: fetchProviders, refetchInterval: 10000 });
  const throughputRequest = toThroughputRequest(throughputSelection);
  const throughput = useQuery({
    queryKey: ["throughput", throughputRequest],
    queryFn: () => fetchThroughput(throughputRequest)
  });
  const fetchedArticles = useQuery({
    queryKey: ["fetched-articles", fetchedWindow],
    queryFn: () => fetchFetchedArticles({ window: fetchedWindow }),
    refetchInterval: 15000
  });
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
    enabled: qualityDetailView === "incorrectly-rejected" && Boolean(lastFilterQualityRunId)
  });
  const incorrectlyAccepted = useQuery({
    queryKey: ["filter-quality", lastFilterQualityRunId, "incorrectly-accepted"],
    queryFn: () => fetchFilterQualityIncorrectlyAccepted(lastFilterQualityRunId ?? ""),
    enabled: qualityDetailView === "incorrectly-accepted" && Boolean(lastFilterQualityRunId)
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
      setQualityDetailView(null);
      queryClient.invalidateQueries({ queryKey: ["filter-quality"] });
    },
    onError: () => {
      setEvaluationStartRequested(false);
      setRequestedEvaluationAction(null);
    }
  });
  const reprocessRejected = useMutation({
    mutationFn: reprocessNewsFetcherRejected,
    onSuccess: (result) => {
      setReprocessResult(result);
      queryClient.invalidateQueries({ queryKey: ["fetched-articles"] });
      queryClient.invalidateQueries({ queryKey: ["backlog"] });
      queryClient.invalidateQueries({ queryKey: ["throughput"] });
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      queryClient.invalidateQueries({ queryKey: ["filter-quality"] });
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

  const chartRows = aggregateThroughputRows(throughput.data);
  const throughputWindowStartAtMs = throughput.data?.window_start_at
    ? new Date(throughput.data.window_start_at).getTime()
    : undefined;
  const throughputWindowEndAtMs = throughput.data?.window_end_at
    ? new Date(throughput.data.window_end_at).getTime()
    : undefined;
  const throughputDataStartAtMs = chartRows[0]?.timestampMs;
  const throughputDataEndAtMs = chartRows[chartRows.length - 1]?.timestampMs;
  const throughputAxisStartAtMs = throughputDataStartAtMs ?? throughputWindowStartAtMs;
  const throughputAxisEndAtMs = throughputDataEndAtMs ?? throughputWindowEndAtMs;
  const throughputAxisDurationMs =
    throughputAxisStartAtMs != null && throughputAxisEndAtMs != null
      ? Math.max(0, throughputAxisEndAtMs - throughputAxisStartAtMs)
      : undefined;
  const throughputUsesDiscretePoints =
    throughput.data?.granularity === "raw" && throughputSelection.window === "15m";
  const throughputTicks = buildThroughputAxisTicks(
    throughput.data?.granularity,
    throughputAxisStartAtMs,
    throughputAxisEndAtMs
  );
  const throughputTickCount =
    throughput.data?.granularity === "raw"
      ? throughputAxisDurationMs != null && throughputAxisDurationMs <= 3 * 24 * 60 * 60 * 1000
    ? 4
        : 6
      : undefined;
  const availabilityNotices = uniqueMessages([
    unavailableMessage(providers.data),
    unavailableMessage(throughput.data),
    unavailableMessage(backlog.data),
    unavailableMessage(deadLetters.data),
    unavailableMessage(filterQuality.data),
    queryUnavailableMessage(incorrectlyRejected.error),
    queryUnavailableMessage(incorrectlyAccepted.error),
    queryUnavailableMessage(productionFilter.error),
    queryUnavailableMessage(testFilter.error),
    queryUnavailableMessage(startFilterQuality.error),
    queryUnavailableMessage(runSimulation.error),
    queryUnavailableMessage(promoteFilter.error),
    queryUnavailableMessage(saveTestFilter.error),
    queryUnavailableMessage(reprocessRejected.error)
  ]);
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
      {availabilityNotices.length > 0 ? (
        <section className="inline-warning-stack" aria-label="Data source warnings">
          {availabilityNotices.map((message) => (
            <DataSourceNotice key={message} text={message} />
          ))}
        </section>
      ) : null}

      <section className="layout">
        <div className="panel panel-large">
          <div className="panel-heading">
            <div>
              <h2>Throughput</h2>
              <span>{formatThroughputWindowSummary(throughput.data, throughputSelection)}</span>
            </div>
          </div>
          <div className="throughput-controls">
            <div className="window-toggle-group" aria-label="Throughput time window">
              {THROUGHPUT_PRESETS.map((preset) => (
                <button
                  type="button"
                  key={preset.window}
                  className={throughputSelection.window === preset.window ? "window-toggle active" : "window-toggle"}
                  onClick={() =>
                    setThroughputSelection((current) => ({ ...current, window: preset.window }))
                  }
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <div className="throughput-end-form">
              <label>
                Window end
                <input
                  type="date"
                  value={throughputSelection.endDate}
                  onChange={(event) =>
                    setThroughputSelection((current) => ({
                      ...current,
                      endDate: event.target.value
                    }))
                  }
                />
              </label>
              <button
                type="button"
                className="secondary-button"
                onClick={() =>
                  setThroughputSelection((current) => ({
                    ...current,
                    endDate: ""
                  }))
                }
                disabled={!throughputSelection.endDate}
              >
                Now
              </button>
            </div>
            <span className="throughput-helper">
              Choose a window size, then optionally move where that window ends. When a date is
              set, the chart ends at the close of that day in your local time.
            </span>
          </div>
          {throughput.data && !throughput.data.available ? (
            <DataSourceNotice text={throughput.data.message ?? "Throughput data unavailable."} compact />
          ) : null}
          <div className="chart">
            {chartRows.length > 0 ? (
              <ResponsiveContainer width="100%" height={290} minWidth={0}>
                <ComposedChart data={chartRows}>
                  <CartesianGrid stroke="#dbe3dc" strokeDasharray="3 3" />
                  <XAxis
                    dataKey="timestampMs"
                    type="number"
                    scale="time"
                    domain={[
                      throughputAxisStartAtMs ?? "dataMin",
                      throughputAxisEndAtMs ?? "dataMax"
                    ]}
                    tickLine={false}
                    axisLine={false}
                    minTickGap={20}
                    tickCount={throughputTickCount}
                    ticks={throughputTicks}
                    tickFormatter={(value) =>
                      formatThroughputAxisTick(
                        Number(value),
                        throughput.data?.granularity,
                        throughputAxisDurationMs
                      )
                    }
                  />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                  <Tooltip content={<ThroughputTooltip />} />
                  {throughputUsesDiscretePoints ? (
                    <>
                      <Scatter dataKey="fetched" fill="#4967d1" />
                      <Scatter dataKey="published" fill="#1d8f6f" />
                      <Scatter dataKey="failed" fill="#b83b3b" />
                    </>
                  ) : (
                    <>
                      <Bar dataKey="fetched" fill="#4967d1" fillOpacity={0.72} radius={[3, 3, 0, 0]} />
                      <Bar dataKey="published" fill="#1d8f6f" fillOpacity={0.82} radius={[3, 3, 0, 0]} />
                      <Bar dataKey="failed" fill="#b83b3b" fillOpacity={0.75} radius={[3, 3, 0, 0]} />
                    </>
                  )}
                </ComposedChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState text={!throughput.data?.available ? "Throughput data unavailable" : "No throughput data yet"} />
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
                <th>Last attempt</th>
                <th>Last non-zero fetch</th>
                <th>Total Published</th>
                <th>Errors</th>
              </tr>
            </thead>
            <tbody>
              {(() => {
                const allProviders = providers.data?.providers ?? [];
                const groupedProviders = PROVIDER_GROUPS.map(group => ({
                  ...group,
                  providers: allProviders.filter(provider => group.pattern.test(provider.source_key))
                }));
                const groupedSourceKeys = new Set(
                  groupedProviders.flatMap(group => group.providers.map(provider => provider.source_key))
                );
                const otherProviders = allProviders.filter(provider => !groupedSourceKeys.has(provider.source_key));
                const rows: ReactNode[] = otherProviders.map(provider => (
                  <tr key={provider.source_key}>
                    <td>{provider.source_key}</td>
                    <td>{formatDate(provider.last_cycle_end_at)}</td>
                    <td>{formatDate(provider.last_fetch_attempt_at)}</td>
                    <td>{formatDate(provider.last_non_zero_fetch_at)}</td>
                    <td>{provider.publish_success_count}</td>
                    <td>{provider.last_error_code ?? provider.fetch_error_count}</td>
                  </tr>
                ));
                groupedProviders.forEach(group => {
                  if (group.providers.length === 0) {
                    return;
                  }
                  const isCollapsed = collapsedGroups.has(group.key);
                  const summary = summarizeProviderGroup(group.providers);
                  rows.push(
                    <tr key={group.key} className="provider-group-header" onClick={() => toggleProviderGroup(group.key)}>
                      <td>
                        <span className="provider-group-chevron">{isCollapsed ? ">" : "v"}</span>
                        {group.key}:* ({group.providers.length})
                      </td>
                      <td>{formatDate(summary.latestCycleEnd)}</td>
                      <td>{formatDate(summary.latestAttempt)}</td>
                      <td>{formatDate(summary.latestNonZero)}</td>
                      <td>{summary.totalPublished}</td>
                      <td>{summary.totalErrors}</td>
                    </tr>
                  );
                  if (!isCollapsed) {
                    group.providers.forEach(provider => {
                      rows.push(
                        <tr key={provider.source_key} className="provider-group-child">
                          <td>{provider.source_key}</td>
                          <td>{formatDate(provider.last_cycle_end_at)}</td>
                          <td>{formatDate(provider.last_fetch_attempt_at)}</td>
                          <td>{formatDate(provider.last_non_zero_fetch_at)}</td>
                          <td>{provider.publish_success_count}</td>
                          <td>{provider.last_error_code ?? provider.fetch_error_count}</td>
                        </tr>
                      );
                    });
                  }
                });
                return rows;
              })()}
            </tbody>
          </table>
          {!providers.data?.providers.length && (
            <EmptyState text={!providers.data?.available ? "Provider telemetry unavailable" : "No provider telemetry yet"} />
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <h2>Fetched Articles</h2>
          <div className="panel-actions">
            <div className="window-toggle-group" aria-label="Fetched articles time window">
              {THROUGHPUT_PRESETS.map((preset) => (
                <button
                  type="button"
                  key={preset.window}
                  className={fetchedWindow === preset.window ? "window-toggle active" : "window-toggle"}
                  onClick={() => setFetchedWindow(preset.window)}
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="secondary-button"
              onClick={() => reprocessRejected.mutate({ window: fetchedWindow })}
              disabled={reprocessRejected.isPending}
            >
              {reprocessRejected.isPending ? "Reprocessing" : "Reprocess rejected"}
            </button>
          </div>
        </div>
        {fetchedArticles.data && !fetchedArticles.data.available ? (
          <DataSourceNotice text={fetchedArticles.data.message ?? "Fetched articles unavailable."} compact />
        ) : null}
        {reprocessResult ? (
          <div className="inline-success">
            {formatReprocessResult(reprocessResult)}
          </div>
        ) : null}
        <FetchedArticlesTable
          items={fetchedArticles.data?.items ?? []}
          loading={!fetchedArticles.data && fetchedArticles.isFetching}
          error={fetchedArticles.isError}
        />
      </section>

      <section className="layout">
        <div className="panel">
          <div className="panel-heading">
            <h2>Backlog</h2>
          </div>
          {backlog.data && !backlog.data.available ? (
            <DataSourceNotice text={backlog.data.message ?? "Backlog data unavailable."} compact />
          ) : null}
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
          {deadLetters.data && !deadLetters.data.available ? (
            <DataSourceNotice text={deadLetters.data.message ?? "Dead-letter data unavailable."} compact />
          ) : null}
          <div className="dead-letter-list">
            {(deadLetters.data?.items ?? []).map((item) => (
              <div className="dead-letter" key={item.obligation_id}>
                <strong>{item.source_key}</strong>
                <span>{item.reason ?? "unknown_error"}</span>
                <small>{formatDate(item.updated_at)}</small>
              </div>
            ))}
            {!deadLetters.data?.items.length && (
              <EmptyState text={!deadLetters.data?.available ? "Dead-letter data unavailable" : "No dead-lettered items"} />
            )}
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
          <div className="filter-quality-actions">
            <label className="toggle-option">
              <input
                type="checkbox"
                checked={acceptedAuditEnabled}
                onChange={(event) => setAcceptedAuditEnabled(event.target.checked)}
                disabled={evaluationRunningOrStarting}
              />
              Evaluate accepted articles
            </label>
            <button
              type="button"
              className="primary-button"
              disabled={evaluationRunningOrStarting}
              onClick={() => startFilterQuality.mutate({ accepted_audit_enabled: acceptedAuditEnabled })}
            >
              {startFilterQuality.isPending || requestedEvaluationAction === "filter-quality" ? "Starting" : "Evaluate production filter"}
            </button>
          </div>
        </div>
        {filterQuality.data && !filterQuality.data.available ? (
          <DataSourceNotice text={filterQuality.data.message ?? "Filter-quality data unavailable."} compact />
        ) : null}
        {filterQuality.data?.last_run ? (
          <FilterQualityMetrics
            run={filterQuality.data.last_run}
            qualityDetailView={qualityDetailView}
            incorrectlyAcceptedItems={incorrectlyAccepted.data?.items ?? []}
            incorrectlyAcceptedLoading={!incorrectlyAccepted.data && incorrectlyAccepted.isFetching}
            incorrectlyAcceptedError={incorrectlyAccepted.isError}
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
            onToggleIncorrectlyRejected={() =>
              setQualityDetailView((current) =>
                current === "incorrectly-rejected" ? null : "incorrectly-rejected"
              )
            }
            onToggleIncorrectlyAccepted={() =>
              setQualityDetailView((current) =>
                current === "incorrectly-accepted" ? null : "incorrectly-accepted"
              )
            }
          />
        ) : (
          <EmptyState text={!filterQuality.data?.available ? "Filter quality data unavailable" : "No filter quality runs yet"} />
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

function DataSourceNotice({ text, compact = false }: { text: string; compact?: boolean }) {
  return <div className={compact ? "inline-warning compact" : "inline-warning"}>{text}</div>;
}

function FilterQualityMetrics({
  run,
  qualityDetailView,
  incorrectlyAcceptedItems,
  incorrectlyAcceptedLoading,
  incorrectlyAcceptedError,
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
  onToggleIncorrectlyRejected,
  onToggleIncorrectlyAccepted
}: {
  run: FilterQualityRunSummary;
  qualityDetailView: "incorrectly-rejected" | "incorrectly-accepted" | null;
  incorrectlyAcceptedItems: FilterQualityIncorrectlyAcceptedItem[];
  incorrectlyAcceptedLoading: boolean;
  incorrectlyAcceptedError: boolean;
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
  onToggleIncorrectlyAccepted: () => void;
}) {
  return (
    <div className="quality-section">
      <div className="quality-groups">
        <QualityGroup title="Overview">
          <QualityValue label="Total quality" value={formatRate(run.total_filter_quality)} />
          <QualityValue label="Dataset input" value={run.dataset_input_count} />
          <QualityValue label="Last finished" value={formatDate(run.finished_at)} />
        </QualityGroup>
        <QualityGroup title="Rejected">
          <QualityValue label="Incorrectly rejected rate" value={formatRate(incorrectlyRejectedRate(run))} />
          <QualityValue label="Rejected evaluated" value={run.rejected_items_evaluated} />
          <QualityValue
            label="Incorrectly rejected"
            value={run.incorrectly_rejected_count}
            buttonDisabled={run.incorrectly_rejected_count === 0}
            onClick={run.incorrectly_rejected_count > 0 ? onToggleIncorrectlyRejected : undefined}
          />
        </QualityGroup>
        <QualityGroup title="Accepted">
          <QualityValue label="Incorrectly accepted rate" value={formatRate(run.incorrectly_accepted_rate_estimate)} />
          <QualityValue label="Accepted audited" value={run.accepted_items_sampled} />
          <QualityValue
            label="Incorrectly accepted"
            value={run.incorrectly_accepted_count}
            buttonDisabled={run.incorrectly_accepted_count === 0}
            onClick={run.incorrectly_accepted_count > 0 ? onToggleIncorrectlyAccepted : undefined}
          />
        </QualityGroup>
        <QualityGroup title="Errors">
          <QualityValue label="Item failures" value={run.item_failed_count} />
          <QualityValue label="Failure reason" value={formatErrorCodes(run.item_error_codes)} />
        </QualityGroup>
      </div>
      {run.status === "failed" && (
        <div className="quality-status-note">
          <QualityValue label="Error" value={run.error_code ?? "unknown_error"} />
        </div>
      )}
      {qualityDetailView === "incorrectly-accepted" && (
        <IncorrectlyAcceptedTable
          items={incorrectlyAcceptedItems}
          lastRun={run}
          loading={incorrectlyAcceptedLoading}
          error={incorrectlyAcceptedError}
        />
      )}
      {qualityDetailView === "incorrectly-rejected" && (
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
  const [manualKeywords, setManualKeywords] = useState<Set<string>>(new Set());
  const [selectedAssessmentId, setSelectedAssessmentId] = useState<string | null>(items[0]?.assessment_id ?? null);
  const mergedKeywords = mergeRecommendedKeywords(items);
  const manualKeywordList = Array.from(manualKeywords).sort();
  const selectedKeywordList = normalizeList([...selectedKeywords, ...manualKeywords]);
  const activeTestFilter = displayedTestFilterDraft({
    lastRun,
    testFilter,
    productionFilter
  });
  useEffect(() => {
    if (!items.length) {
      setSelectedAssessmentId(null);
      return;
    }
    if (!selectedAssessmentId || !items.some((item) => item.assessment_id === selectedAssessmentId)) {
      setSelectedAssessmentId(items[0].assessment_id);
    }
  }, [items, selectedAssessmentId]);
  const selectedItem =
    items.find((item) => item.assessment_id === selectedAssessmentId) ?? items[0];

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
  const clearSelection = () => {
    setSelectedKeywords(new Set());
    setManualKeywords(new Set());
  };
  const addManualKeyword = (keyword: string) => {
    const normalized = normalizeKeywordText(keyword);
    if (!normalized) {
      return;
    }
    setManualKeywords((current) => new Set([...current, normalized]));
  };
  const removeManualKeyword = (keyword: string) => {
    setManualKeywords((current) => {
      const next = new Set(current);
      next.delete(keyword);
      return next;
    });
  };
  const saveSelectedToTestFilter = () => {
    if (!activeTestFilter) {
      return;
    }
    const include = normalizeList([...activeTestFilter.include_keywords, ...selectedKeywordList]);
    saveTestFilter({ ...activeTestFilter, config_role: "test", status: "active", include_keywords: include });
    setSelectedKeywords(new Set());
    setManualKeywords(new Set());
  };
  return (
    <div className="quality-details">
      <div className="keyword-actions">
        <div className="keyword-section">
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
          <div className="keyword-entry">
            <button type="button" className="secondary-button" onClick={selectAll} disabled={!mergedKeywords.length}>
              Select all
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={clearSelection}
              disabled={!selectedKeywords.size}
            >
              Clear selection
            </button>
          </div>
        </div>
        <div className="keyword-section">
          {manualKeywordList.length > 0 && (
            <div className="manual-keyword-summary">
              <strong>Manual keywords</strong>
              <div className="keyword-list">
                {manualKeywordList.map((keyword) => (
                  <button
                    type="button"
                    className="keyword-chip manual-chip"
                    key={`summary-manual-${keyword}`}
                    onClick={() => removeManualKeyword(keyword)}
                    title="Remove manual keyword"
                  >
                    {keyword}
                    <span aria-hidden="true">x</span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="keyword-entry">
            <button type="button" className="primary-button" onClick={saveSelectedToTestFilter} disabled={!selectedKeywordList.length || !activeTestFilter || savePending}>
            {savePending ? "Saving" : "Add to test filter"}
            </button>
          </div>
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
      <div className="review-layout">
        <div className="table-wrap review-list">
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
                <tr
                  key={item.assessment_id}
                  className={item.assessment_id === selectedItem?.assessment_id ? "review-row selected" : "review-row"}
                  onClick={() => setSelectedAssessmentId(item.assessment_id)}
                >
                  <td className="article-cell">
                    <a href={item.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
                      {item.headline}
                    </a>
                    {displayedMatchedArticle(item, lastRun).url && displayedMatchedArticle(item, lastRun).headline && (
                      <small>
                        Duplicate of{" "}
                        <a
                          href={displayedMatchedArticle(item, lastRun).url ?? undefined}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(event) => event.stopPropagation()}
                        >
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
        <ArticleReviewPanel
          item={selectedItem}
          lastRun={lastRun}
          selectedKeywords={selectedKeywords}
          manualKeywords={manualKeywords}
          updateSelected={updateSelected}
          addManualKeyword={addManualKeyword}
          removeManualKeyword={removeManualKeyword}
        />
      </div>
    </div>
  );
}

function QualityGroup({
  title,
  children
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="quality-group">
      <div className="quality-group-title">{title}</div>
      <div className="quality-grid">{children}</div>
    </section>
  );
}

function IncorrectlyAcceptedTable({
  items,
  lastRun,
  loading,
  error
}: {
  items: FilterQualityIncorrectlyAcceptedItem[];
  lastRun: FilterQualityRunSummary;
  loading: boolean;
  error: boolean;
}) {
  const [selectedAssessmentId, setSelectedAssessmentId] = useState<string | null>(items[0]?.assessment_id ?? null);

  useEffect(() => {
    if (!items.length) {
      setSelectedAssessmentId(null);
      return;
    }
    if (!selectedAssessmentId || !items.some((item) => item.assessment_id === selectedAssessmentId)) {
      setSelectedAssessmentId(items[0].assessment_id);
    }
  }, [items, selectedAssessmentId]);

  const selectedItem = items.find((item) => item.assessment_id === selectedAssessmentId) ?? items[0];

  if (loading) {
    return <EmptyState text="Loading incorrectly accepted records" />;
  }
  if (error) {
    return <EmptyState text="Incorrectly accepted records unavailable" />;
  }
  if (!items.length) {
    return <EmptyState text="No incorrectly accepted records" />;
  }

  return (
    <div className="quality-details">
      <div className="quality-details-header">
        <strong>Accepted articles flagged as low-value noise</strong>
        <span>{items.length} articles</span>
      </div>
      <div className="review-layout">
        <div className="table-wrap review-list">
          <table>
            <thead>
              <tr>
                <th>Article</th>
                <th>Published</th>
                <th>Source</th>
                <th>Cause</th>
                <th>Confidence</th>
                <th>Suggested action</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.assessment_id}
                  className={item.assessment_id === selectedItem?.assessment_id ? "review-row selected" : "review-row"}
                  onClick={() => setSelectedAssessmentId(item.assessment_id)}
                >
                  <td className="article-cell">
                    <a href={item.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
                      {item.headline}
                    </a>
                    {item.rationale && <small>{item.rationale}</small>}
                  </td>
                  <td>{formatDate(item.published_at)}</td>
                  <td>{item.source}</td>
                  <td>{formatCause(item.probable_cause)}</td>
                  <td>{formatConfidence(item.classification_confidence)}</td>
                  <td className="solution-cell">{item.improvement_suggestion ?? "n/a"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <IncorrectlyAcceptedReviewPanel item={selectedItem} lastRun={lastRun} />
      </div>
    </div>
  );
}

function ArticleReviewPanel({
  item,
  lastRun,
  selectedKeywords,
  manualKeywords,
  updateSelected,
  addManualKeyword,
  removeManualKeyword
}: {
  item?: FilterQualityIncorrectlyRejectedItem;
  lastRun: FilterQualityRunSummary;
  selectedKeywords: Set<string>;
  manualKeywords: Set<string>;
  updateSelected: (keyword: string, checked: boolean) => void;
  addManualKeyword: (keyword: string) => void;
  removeManualKeyword: (keyword: string) => void;
}) {
  const [selectedText, setSelectedText] = useState("");
  const manualKeywordList = Array.from(manualKeywords).sort();
  useEffect(() => {
    setSelectedText("");
    window.getSelection()?.removeAllRanges();
  }, [item?.assessment_id]);
  const updateSelectedText = (container: HTMLElement) => {
    const selection = window.getSelection();
    const rawText = selection?.toString() ?? "";
    if (!rawText.trim()) {
      setSelectedText("");
      return;
    }
    const anchorNode = selection?.anchorNode;
    if (!anchorNode || !container.contains(anchorNode)) {
      setSelectedText("");
      return;
    }
    setSelectedText(normalizeKeywordText(rawText));
  };
  const useSelectedText = () => {
    addManualKeyword(selectedText);
    setSelectedText("");
    window.getSelection()?.removeAllRanges();
  };
  if (!item) {
    return <aside className="review-panel"><EmptyState text="Select an article to review" /></aside>;
  }
  const matchedArticle = displayedMatchedArticle(item, lastRun);
  return (
    <aside
      className="review-panel"
      aria-label="Selected article review"
      onMouseUp={(event) => updateSelectedText(event.currentTarget)}
      onKeyUp={(event) => updateSelectedText(event.currentTarget)}
    >
      <div className="review-panel-heading">
        <span>{item.source}</span>
        <strong>{formatDate(item.published_at)}</strong>
      </div>
      <div className="review-title">{item.headline}</div>
      <div className="review-links">
        <a href={item.url} target="_blank" rel="noreferrer">
          Open article
        </a>
      </div>
      <div className="review-meta">
        <span>{displayedRejectionReason(item, lastRun) ?? "n/a"}</span>
        <span>{formatCause(displayedCause(item, lastRun))}</span>
      </div>
      {selectedText && (
        <div className="selection-actions">
          <span>{selectedText}</span>
          <button type="button" className="secondary-button" onClick={useSelectedText}>
            Use as keyword
          </button>
        </div>
      )}
      <section className="review-section">
        <h3>Summary</h3>
        <p className="summary-text">{item.summary || "No article summary available."}</p>
      </section>
      {matchedArticle.url && matchedArticle.headline && (
        <section className="review-section">
          <h3>Duplicate match</h3>
          <p className="summary-text">
            <a href={matchedArticle.url} target="_blank" rel="noreferrer">
              {matchedArticle.headline}
            </a>
            {matchedArticle.source ? ` (${matchedArticle.source})` : ""}
            {matchedArticle.published_at ? `, ${formatDate(matchedArticle.published_at)}` : ""}
          </p>
        </section>
      )}
      <section className="review-section">
        <h3>Recommended keywords</h3>
        <div className="keyword-list">
          {item.recommended_include_keywords.map((keyword) => (
            <label className="keyword-chip" key={`${item.assessment_id}-review-${keyword}`}>
              <input
                type="checkbox"
                checked={selectedKeywords.has(keyword)}
                onChange={(event) => updateSelected(keyword, event.target.checked)}
              />
              {keyword}
            </label>
          ))}
          {!item.recommended_include_keywords.length && <span className="muted">No structured keyword recommendations</span>}
        </div>
      </section>
      {manualKeywordList.length > 0 && (
        <section className="review-section">
          <h3>Manual keywords</h3>
          <div className="keyword-list">
            {manualKeywordList.map((keyword) => (
              <button
                type="button"
                className="keyword-chip manual-chip"
                key={`manual-${keyword}`}
                onClick={() => removeManualKeyword(keyword)}
                title="Remove selected keyword"
              >
                {keyword}
                <span aria-hidden="true">x</span>
              </button>
            ))}
          </div>
        </section>
      )}
      {item.rationale && (
        <section className="review-section">
          <h3>Rationale</h3>
          <p className="summary-text">{item.rationale}</p>
        </section>
      )}
      {displayedSolution(item, lastRun) && (
        <section className="review-section">
          <h3>Suggested action</h3>
          <p className="summary-text">{displayedSolution(item, lastRun)}</p>
        </section>
      )}
    </aside>
  );
}

function IncorrectlyAcceptedReviewPanel({
  item,
  lastRun
}: {
  item?: FilterQualityIncorrectlyAcceptedItem;
  lastRun: FilterQualityRunSummary;
}) {
  if (!item) {
    return <aside className="review-panel"><EmptyState text="Select an article to review" /></aside>;
  }
  return (
    <aside className="review-panel" aria-label="Selected accepted article review">
      <div className="review-panel-heading">
        <span>{item.source}</span>
        <strong>{formatDate(item.published_at)}</strong>
      </div>
      <div className="review-title">{item.headline}</div>
      <div className="review-links">
        <a href={item.url} target="_blank" rel="noreferrer">
          Open article
        </a>
      </div>
      <div className="review-meta">
        <span>{formatEvaluationSubject(lastRun.evaluation_subject)}</span>
        <span>{formatCause(item.probable_cause)}</span>
        <span>{formatConfidence(item.classification_confidence)}</span>
      </div>
      <section className="review-section">
        <h3>Summary</h3>
        <p className="summary-text">{item.summary || "No article summary available."}</p>
      </section>
      {item.rationale && (
        <section className="review-section">
          <h3>Rationale</h3>
          <p className="summary-text">{item.rationale}</p>
        </section>
      )}
      {item.improvement_suggestion && (
        <section className="review-section">
          <h3>Suggested action</h3>
          <p className="summary-text">{item.improvement_suggestion}</p>
        </section>
      )}
    </aside>
  );
}

function FetchedArticlesTable({
  items,
  loading,
  error
}: {
  items: FetchedArticle[];
  loading: boolean;
  error: boolean;
}) {
  const [selectedArticleId, setSelectedArticleId] = useState<string | null>(items[0]?.article_id ?? null);

  useEffect(() => {
    if (!items.length) {
      setSelectedArticleId(null);
      return;
    }
    if (!selectedArticleId || !items.some((item) => item.article_id === selectedArticleId)) {
      setSelectedArticleId(items[0].article_id);
    }
  }, [items, selectedArticleId]);

  const selectedItem = items.find((item) => item.article_id === selectedArticleId) ?? items[0];

  if (loading) {
    return <EmptyState text="Loading fetched articles" />;
  }
  if (error) {
    return <EmptyState text="Fetched articles unavailable" />;
  }
  if (!items.length) {
    return <EmptyState text="No fetched articles in this window" />;
  }

  return (
    <div className="quality-details">
      <div className="quality-details-header">
        <strong>Articles persisted by the fetcher</strong>
        <span>{items.length} articles</span>
      </div>
      <div className="review-layout">
        <div className="table-wrap review-list">
          <table>
            <thead>
              <tr>
                <th>Article</th>
                <th>Published</th>
                <th>Fetched</th>
                <th>Tickers</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.article_id}
                  className={item.article_id === selectedItem?.article_id ? "review-row selected" : "review-row"}
                  onClick={() => setSelectedArticleId(item.article_id)}
                >
                  <td className="article-cell">
                    <a href={item.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
                      {item.headline}
                    </a>
                    <small>{item.source}</small>
                  </td>
                  <td>{formatDate(item.published_at)}</td>
                  <td>{formatDate(item.fetched_at)}</td>
                  <td>{item.tickers.length ? item.tickers.join(", ") : "n/a"}</td>
                  <td>{formatOutcome(item)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <FetchedArticleReviewPanel item={selectedItem} />
      </div>
    </div>
  );
}

function FetchedArticleReviewPanel({ item }: { item?: FetchedArticle }) {
  if (!item) {
    return <aside className="review-panel"><EmptyState text="Select an article to review" /></aside>;
  }
  return (
    <aside className="review-panel" aria-label="Selected fetched article review">
      <div className="review-panel-heading">
        <span>{item.source}</span>
        <strong>{formatDate(item.published_at)}</strong>
      </div>
      <div className="review-title">{item.headline}</div>
      <div className="review-links">
        <a href={item.url} target="_blank" rel="noreferrer">
          Open article
        </a>
      </div>
      <div className="review-meta">
        <span>{formatOutcome(item)}</span>
        {item.rejection_reason_code && <span>{item.rejection_reason_code}</span>}
        <span>Fetched {formatDate(item.fetched_at)}</span>
      </div>
      <section className="review-section">
        <h3>Summary</h3>
        <p className="summary-text">{item.summary || "No article summary available."}</p>
      </section>
      <section className="review-section">
        <h3>Tickers</h3>
        <p className="summary-text">{item.tickers.length ? item.tickers.join(", ") : "No tickers tagged."}</p>
      </section>
    </aside>
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
  const [includeText, setIncludeText] = useState(config.include_keywords.join(", "));
  const [excludeText, setExcludeText] = useState(config.exclude_keywords.join(", "));
  const [watchlistText, setWatchlistText] = useState(config.watchlist_tickers.join(", "));
  const configSignature = [
    config.filter_config_id ?? "",
    config.created_from_run_id ?? "",
    config.include_keywords.join("\n"),
    config.exclude_keywords.join("\n"),
    config.watchlist_tickers.join("\n"),
    config.dedupe_algorithm,
    config.dedupe_similarity_threshold,
    config.dedupe_lookback_hours
  ].join("\u001f");
  useEffect(() => {
    setDraft(config);
    setIncludeText(config.include_keywords.join(", "));
    setExcludeText(config.exclude_keywords.join(", "));
    setWatchlistText(config.watchlist_tickers.join(", "));
  }, [configSignature]);
  const saveDraft = () => {
    saveTestFilter({
      ...draft,
      config_role: "test",
      status: "active",
      include_keywords: normalizeList(includeText.split(",")),
      exclude_keywords: normalizeList(excludeText.split(",")),
      watchlist_tickers: normalizeTickers(watchlistText.split(","))
    });
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
          <textarea value={includeText} onChange={(event) => setIncludeText(event.target.value)} />
        </label>
        <label>
          Exclude keywords
          <textarea value={excludeText} onChange={(event) => setExcludeText(event.target.value)} />
        </label>
        <label>
          Watchlist tickers
          <textarea value={watchlistText} onChange={(event) => setWatchlistText(event.target.value)} />
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
          <button type="button" className="primary-button" onClick={saveDraft} disabled={savePending}>
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

function formatReprocessResult(result: NewsFetcherReprocessRejectedResponse) {
  return [
    `Reprocessed ${result.scanned_rejected_count} rejected`,
    `${result.newly_accepted_count} newly accepted`,
    `${result.queued_publication_obligation_count} queued`
  ].join("; ");
}

function summarizeProviderGroup(providers: ProviderStatus[]) {
  return {
    latestCycleEnd: latestTimestamp(providers.map(provider => provider.last_cycle_end_at)),
    latestAttempt: latestTimestamp(providers.map(provider => provider.last_fetch_attempt_at)),
    latestNonZero: latestTimestamp(providers.map(provider => provider.last_non_zero_fetch_at)),
    totalPublished: providers.reduce((sum, provider) => sum + provider.publish_success_count, 0),
    totalErrors: providers.reduce((sum, provider) => sum + provider.fetch_error_count, 0)
  };
}

function latestTimestamp(values: Array<string | null | undefined>) {
  return values.filter((value): value is string => Boolean(value)).sort().at(-1);
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

function formatThroughputAxisTick(
  value: number,
  granularity?: ThroughputResponse["granularity"],
  windowDurationMs?: number
) {
  const date = new Date(value);
  if (granularity === "day") {
    return date.toLocaleDateString([], {
      month: "short",
      day: "numeric"
    });
  }
  if (granularity === "hour") {
    if (windowDurationMs != null && windowDurationMs <= 24 * 60 * 60 * 1000) {
      return date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit"
      });
    }
    return date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }
  if (windowDurationMs != null && windowDurationMs <= 24 * 60 * 60 * 1000) {
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit"
    });
  }
  if (windowDurationMs != null && windowDurationMs <= 3 * 24 * 60 * 60 * 1000) {
    return date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }
  return date.toLocaleDateString([], {
    month: "short",
    day: "numeric"
  });
}

function buildThroughputAxisTicks(
  granularity?: ThroughputResponse["granularity"],
  windowStartAtMs?: number,
  windowEndAtMs?: number
) {
  if (granularity === "day") {
    return buildAlignedTicks(windowStartAtMs, windowEndAtMs, "day", 1);
  }
  if (granularity === "hour") {
    const durationMs =
      windowStartAtMs != null && windowEndAtMs != null
        ? Math.max(0, windowEndAtMs - windowStartAtMs)
        : undefined;
    if (durationMs == null) {
      return undefined;
    }
    const stepHours = durationMs <= 24 * 60 * 60 * 1000 ? 3 : durationMs <= 3 * 24 * 60 * 60 * 1000 ? 6 : 24;
    return buildAlignedTicks(windowStartAtMs, windowEndAtMs, "hour", stepHours);
  }
  return undefined;
}

function buildAlignedTicks(
  windowStartAtMs: number | undefined,
  windowEndAtMs: number | undefined,
  unit: "hour" | "day",
  step: number
) {
  if (windowStartAtMs == null || windowEndAtMs == null) {
    return undefined;
  }
  const start = new Date(windowStartAtMs);
  const end = new Date(windowEndAtMs);

  const ticks: number[] = [];
  const cursor = new Date(start);
  if (unit === "day") {
    cursor.setHours(0, 0, 0, 0);
    if (cursor.getTime() < start.getTime()) {
      cursor.setDate(cursor.getDate() + step);
    }
  } else {
    cursor.setMinutes(0, 0, 0);
    const currentHour = cursor.getHours();
    const alignedHour = Math.ceil(currentHour / step) * step;
    cursor.setHours(alignedHour, 0, 0, 0);
    if (cursor.getTime() < start.getTime()) {
      cursor.setHours(cursor.getHours() + step);
    }
  }

  while (cursor.getTime() < end.getTime()) {
    ticks.push(cursor.getTime());
    if (unit === "day") {
      cursor.setDate(cursor.getDate() + step);
    } else {
      cursor.setHours(cursor.getHours() + step);
    }
  }

  return ticks.length > 0 ? ticks : undefined;
}

function formatThroughputTooltipLabel(value: number) {
  return new Date(value).toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function ThroughputTooltip({
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
      label: formatThroughputSeriesLabel(String(entry.dataKey)),
      value: entry.value
    }));
  if (!items.length) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <strong>{formatThroughputTooltipLabel(Number(label))}</strong>
      {items.map((item) => (
        <div key={item.label}>
          {item.label}: {item.value}
        </div>
      ))}
    </div>
  );
}

function formatThroughputSeriesLabel(value: string) {
  if (value === "fetched") {
    return "Fetched";
  }
  if (value === "published") {
    return "Published";
  }
  if (value === "failed") {
    return "Failed";
  }
  return value;
}

function formatThroughputWindowSummary(
  _response: ThroughputResponse | undefined,
  selection: ThroughputSelection
) {
  const label = `Last ${selection.window}`;
  if (!selection.endDate) {
    return `${label} ending now`;
  }
  return `${label} ending ${formatShortDate(windowEndDateToInclusiveIso(selection.endDate))}`;
}

function formatShortDateTime(value: string) {
  return new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function formatShortDate(value: string) {
  return new Date(value).toLocaleDateString([], {
    year: "numeric",
    month: "short",
    day: "numeric"
  });
}

function formatRate(value?: number | null) {
  if (value == null) {
    return "n/a";
  }
  return `${(value * 100).toFixed(2)}%`;
}

function incorrectlyRejectedRate(run: FilterQualityRunSummary) {
  if (run.rejected_items_evaluated <= 0) {
    return null;
  }
  return run.incorrectly_rejected_count / run.rejected_items_evaluated;
}

function formatConfidence(value?: number | null) {
  if (value == null) {
    return "n/a";
  }
  return `${(value * 100).toFixed(0)}% confidence`;
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

function unavailableMessage(
  response:
    | { available: boolean; message?: string | null }
    | undefined
) {
  if (!response || response.available) {
    return null;
  }
  return response.message ?? "Data source unavailable.";
}

function queryUnavailableMessage(error: unknown) {
  if (!(error instanceof ApiError) || error.status !== 503) {
    return null;
  }
  return error.message || "Data source unavailable.";
}

function uniqueMessages(values: Array<string | null>) {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value))));
}

function formatCause(value?: string | null) {
  if (!value) {
    return "n/a";
  }
  return value.replaceAll("_", " ");
}

function formatOutcome(item: FetchedArticle) {
  if (item.filter_outcome === "rejected") {
    return item.matched_article_id ? "duplicate" : "rejected";
  }
  if (item.filter_outcome === "accepted") {
    return "accepted";
  }
  return "pending";
}

function aggregateThroughputRows(response?: ThroughputResponse) {
  if (!response) {
    return [];
  }
  const totals = new Map<
    string,
    {
      timestamp: string;
      timestampMs: number;
      fetched: number;
      published: number;
      failed: number;
    }
  >();
  for (const bucket of response.buckets) {
    const current = totals.get(bucket.window_start) ?? {
      timestamp: bucket.window_start,
      timestampMs: new Date(bucket.window_start).getTime(),
      fetched: 0,
      published: 0,
      failed: 0
    };
    current.fetched += bucket.fetch_count;
    current.published += bucket.publish_success_count;
    current.failed += bucket.publish_error_count;
    totals.set(bucket.window_start, current);
  }
  return Array.from(totals.values()).sort(
    (left, right) => left.timestampMs - right.timestampMs
  );
}

function toThroughputRequest(selection: ThroughputSelection): ThroughputRequest {
  if (!selection.endDate) {
    return { window: selection.window };
  }
  const endAt = dateInputExclusiveEndToIso(selection.endDate);
  return {
    window: selection.window,
    startAt: subtractThroughputWindow(endAt, selection.window),
    endAt
  };
}

function dateInputExclusiveEndToIso(value: string) {
  const endDate = new Date(`${value}T00:00:00`);
  endDate.setDate(endDate.getDate() + 1);
  return endDate.toISOString();
}

function windowEndDateToInclusiveIso(value: string) {
  return new Date(`${value}T23:59:59`).toISOString();
}

function subtractThroughputWindow(endAtIso: string, window: ThroughputPresetWindow) {
  const endAt = new Date(endAtIso);
  const startAt = new Date(endAt.getTime());
  switch (window) {
    case "15m":
      startAt.setUTCMinutes(startAt.getUTCMinutes() - 15);
      break;
    case "1h":
      startAt.setUTCHours(startAt.getUTCHours() - 1);
      break;
    case "1d":
      startAt.setUTCDate(startAt.getUTCDate() - 1);
      break;
    case "7d":
      startAt.setUTCDate(startAt.getUTCDate() - 7);
      break;
    case "30d":
      startAt.setUTCDate(startAt.getUTCDate() - 30);
      break;
  }
  return startAt.toISOString();
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

function normalizeKeywordText(value: string) {
  return value.trim().replace(/\s+/g, " ").toLowerCase();
}

function normalizeList(values: string[]) {
  return Array.from(
    new Set(values.map(normalizeKeywordText).filter(Boolean))
  ).sort();
}

function normalizeTickers(values: string[]) {
  return Array.from(
    new Set(values.map((value) => value.trim().toUpperCase()).filter(Boolean))
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
