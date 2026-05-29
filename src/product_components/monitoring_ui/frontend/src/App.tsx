import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  fetchBacklog,
  fetchDeadLetters,
  fetchHealth,
  fetchProviders,
  fetchThroughput,
  type HealthState
} from "./api";

export function App() {
  const health = useQuery({ queryKey: ["health"], queryFn: fetchHealth });
  const providers = useQuery({ queryKey: ["providers"], queryFn: fetchProviders, refetchInterval: 10000 });
  const throughput = useQuery({ queryKey: ["throughput", "1h"], queryFn: () => fetchThroughput("1h") });
  const backlog = useQuery({ queryKey: ["backlog"], queryFn: fetchBacklog, refetchInterval: 20000 });
  const deadLetters = useQuery({ queryKey: ["dead-letter"], queryFn: fetchDeadLetters, refetchInterval: 20000 });

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
