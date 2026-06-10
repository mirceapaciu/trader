import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  fetchBacklog,
  fetchDeadLetters,
  fetchFilterQualityIncorrectlyRejected,
  fetchHealth,
  fetchFilterQualityStatus,
  fetchProviders,
  fetchThroughput,
  startFilterQualityRun,
  type FilterQualityIncorrectlyRejectedItem,
  type FilterQualityRunSummary,
  type HealthState
} from "./api";

export function App() {
  const queryClient = useQueryClient();
  const [incorrectlyRejectedOpen, setIncorrectlyRejectedOpen] = useState(false);
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
  const startFilterQuality = useMutation({
    mutationFn: startFilterQualityRun,
    onSuccess: () => {
      setIncorrectlyRejectedOpen(false);
      queryClient.invalidateQueries({ queryKey: ["filter-quality"] });
    }
  });

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
            <span className={`state-pill ${filterQualityState(filterQuality.data?.running_run, filterQuality.data?.last_run)}`}>
              {filterQualityState(filterQuality.data?.running_run, filterQuality.data?.last_run)}
            </span>
          </div>
          <button
            type="button"
            className="primary-button"
            disabled={Boolean(filterQuality.data?.running_run) || startFilterQuality.isPending}
            onClick={() => startFilterQuality.mutate()}
          >
            {startFilterQuality.isPending ? "Starting" : "Run filter quality"}
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
            incorrectlyRejectedLoading={incorrectlyRejected.isLoading || incorrectlyRejected.isFetching}
            incorrectlyRejectedError={incorrectlyRejected.isError}
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
  onToggleIncorrectlyRejected
}: {
  run: FilterQualityRunSummary;
  incorrectlyRejectedOpen: boolean;
  incorrectlyRejectedItems: FilterQualityIncorrectlyRejectedItem[];
  incorrectlyRejectedLoading: boolean;
  incorrectlyRejectedError: boolean;
  onToggleIncorrectlyRejected: () => void;
}) {
  return (
    <div className="quality-section">
      <div className="quality-grid">
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
        <QualityValue label="Item failures" value={run.item_failed_count} />
        <QualityValue label="Dataset input" value={run.dataset_input_count} />
        <QualityValue label="Last finished" value={formatDate(run.finished_at)} />
        {run.item_failed_count > 0 && <QualityValue label="Failure reason" value={formatErrorCodes(run.item_error_codes)} />}
        {run.status === "failed" && <QualityValue label="Error" value={run.error_code ?? "unknown_error"} />}
      </div>
      {incorrectlyRejectedOpen && (
        <IncorrectlyRejectedTable
          items={incorrectlyRejectedItems}
          loading={incorrectlyRejectedLoading}
          error={incorrectlyRejectedError}
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
  loading,
  error
}: {
  items: FilterQualityIncorrectlyRejectedItem[];
  loading: boolean;
  error: boolean;
}) {
  if (loading) {
    return <EmptyState text="Loading incorrectly rejected records" />;
  }
  if (error) {
    return <EmptyState text="Incorrectly rejected records unavailable" />;
  }
  if (!items.length) {
    return <EmptyState text="No incorrectly rejected records" />;
  }
  return (
    <div className="quality-details table-wrap">
      <table>
        <thead>
          <tr>
            <th>Article</th>
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
                {item.rationale && <small>{item.rationale}</small>}
              </td>
              <td>{formatDate(item.published_at)}</td>
              <td>{item.source}</td>
              <td>{item.rejection_reason_code ?? "n/a"}</td>
              <td>{formatCause(item.probable_cause)}</td>
              <td className="solution-cell">{item.improvement_suggestion ?? "n/a"}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
