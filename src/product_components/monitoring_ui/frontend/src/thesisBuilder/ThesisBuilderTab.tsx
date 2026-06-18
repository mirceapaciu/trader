import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  fetchThesisBuilderMetrics,
  type ThesisBuilderDeadLetterItem,
  type ThesisBuilderMetricsResponse,
  type ThesisBuilderPendingWindow,
  type ThroughputPresetWindow
} from "../api";

const WINDOW_PRESETS: Array<{ label: string; window: ThroughputPresetWindow }> = [
  { label: "15m", window: "15m" },
  { label: "1h", window: "1h" },
  { label: "1d", window: "1d" },
  { label: "7d", window: "7d" },
  { label: "30d", window: "30d" }
];

export function ThesisBuilderTab() {
  const [window, setWindow] = useState<ThroughputPresetWindow>("1d");
  const metrics = useQuery({
    queryKey: ["thesis-builder", "metrics", window],
    queryFn: () => fetchThesisBuilderMetrics(window),
    refetchInterval: 15000
  });
  const data = metrics.data;

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">ThesisBuilder</p>
          <h1>Thesis Monitoring</h1>
        </div>
        <div className="timestamp">
          Last refresh
          <strong>{formatDate(data?.generated_at)}</strong>
        </div>
      </header>

      <section className="panel thesis-window-panel">
        <div className="panel-heading">
          <div>
            <h2>Window</h2>
            <span>{formatWindowSummary(data)}</span>
          </div>
        </div>
        <div className="window-toggle-group" aria-label="ThesisBuilder metrics time window">
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

      {metrics.isError && <div className="inline-error">{metrics.error.message}</div>}
      {data && !data.available && <div className="inline-error">{data.message ?? "ThesisBuilder telemetry unavailable."}</div>}

      <section className="thesis-kpi-grid">
        <MetricTile label="Articles processed" value={formatInteger(data?.articles_processed_count)} />
        <MetricTile label="Market moving" value={formatInteger(data?.market_moving_articles_count)} />
        <MetricTile label="Included in cards" value={formatInteger(data?.articles_included_in_cards_count)} />
        <MetricTile label="Too old" value={formatInteger(data?.stale_articles_count)} />
        <MetricTile label="Created cards" value={formatInteger(data?.created_thesis_cards_count)} />
        <MetricTile label="Pending cards" value={formatInteger(data?.pending_thesis_cards_count)} />
        <MetricTile label="Dead letters" value={formatInteger(data?.dead_letter_count)} />
        <MetricTile label="Oldest pending age" value={formatDuration(data?.oldest_pending_age_seconds)} />
        <MetricTile label="Avg pending age" value={formatDuration(data?.average_pending_age_seconds)} />
        <MetricTile label="Min time to expiry" value={formatDuration(data?.minimum_pending_expires_in_seconds)} />
        <MetricTile label="Avg time to expiry" value={formatDuration(data?.average_pending_expires_in_seconds)} />
      </section>

      <section className="layout thesis-layout">
        <PendingWindowsPanel windows={data?.pending_windows ?? []} />
        <StaleEvidencePanel data={data} />
        <DeadLetterPanel data={data} />
      </section>
    </main>
  );
}

function PendingWindowsPanel({ windows }: { windows: ThesisBuilderPendingWindow[] }) {
  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Pending Thesis Cards</h2>
          <span>Collecting evidence windows</span>
        </div>
      </div>
      {windows.length === 0 ? (
        <div className="empty">No pending thesis-card windows.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Strategy</th>
                <th>Direction</th>
                <th>Age</th>
                <th>Expires in</th>
                <th>Last evidence</th>
              </tr>
            </thead>
            <tbody>
              {windows.map((window) => (
                <tr key={window.window_id}>
                  <td>
                    <strong>{window.ticker}</strong>
                    <span className="table-subtext">{window.exchange_code}</span>
                  </td>
                  <td>{formatToken(window.strategy)}</td>
                  <td>{window.direction ? formatToken(window.direction) : "n/a"}</td>
                  <td>{formatDuration(window.pending_age_seconds)}</td>
                  <td>{formatDuration(window.expires_in_seconds)}</td>
                  <td>{formatDate(window.last_evidence_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function StaleEvidencePanel({ data }: { data?: ThesisBuilderMetricsResponse }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Stale Evidence</h2>
          <span>Operator label: stale</span>
        </div>
      </div>
      <div className="quality-grid thesis-stale-grid">
        <QualityValue label="Missed cards" value={formatInteger(data?.missed_stale_thesis_cards_count)} />
        <QualityValue label="Avg exceedance" value={formatDuration(data?.stale_evidence_exceeded_avg_seconds)} />
        <QualityValue label="P95 exceedance" value={formatDuration(data?.stale_evidence_exceeded_p95_seconds)} />
        <QualityValue label="Max exceedance" value={formatDuration(data?.stale_evidence_exceeded_max_seconds)} />
      </div>
    </section>
  );
}

function DeadLetterPanel({ data }: { data?: ThesisBuilderMetricsResponse }) {
  const items = data?.recent_dead_letters ?? [];
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Dead Letters</h2>
          <span>Recent ThesisBuilder consumer failures</span>
        </div>
      </div>
      <div className="dead-letter-list">
        {items.map((item) => (
          <DeadLetterRow key={item.source_message_id} item={item} />
        ))}
        {items.length === 0 ? (
          <EmptyState
            text={!data?.available ? "ThesisBuilder dead-letter data unavailable" : "No ThesisBuilder dead letters"}
          />
        ) : null}
      </div>
    </section>
  );
}

function DeadLetterRow({ item }: { item: ThesisBuilderDeadLetterItem }) {
  return (
    <div className="dead-letter">
      <strong>{item.article_id}</strong>
      <span>{item.error_code ?? "unknown_error"}</span>
      <small>{formatDate(item.failed_at)}</small>
    </div>
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

function QualityValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="quality-value">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function formatInteger(value?: number | null) {
  return value == null ? "n/a" : value.toLocaleString();
}

function formatDuration(seconds?: number | null) {
  if (seconds == null) {
    return "n/a";
  }
  const absSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(absSeconds / 3600);
  const minutes = Math.floor((absSeconds % 3600) / 60);
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

function formatDate(value?: string | null) {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatToken(value: string) {
  return value.replaceAll("_", " ");
}

function formatWindowSummary(data?: ThesisBuilderMetricsResponse) {
  if (!data) {
    return "Loading metrics";
  }
  return `${formatDate(data.window_start_at)} to ${formatDate(data.window_end_at)}`;
}
