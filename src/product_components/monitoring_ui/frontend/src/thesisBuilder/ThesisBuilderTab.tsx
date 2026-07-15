import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";

import {
  fetchNewsAnalyses,
  fetchThesisBuilderConfig,
  fetchReprocessStatus,
  fetchThesisBuilderMetrics,
  fetchThesisBuilderThroughput,
  fetchThesisCardArticles,
  fetchThesisCards,
  fetchWindowArticles,
  reprocessThesisBuilder,
  type NewsAnalysisItem,
  type ThesisBuilderConsumerHealth,
  type ThesisBuilderConfigResponse,
  type ThesisBuilderDeadLetterItem,
  type ThesisBuilderMetricsResponse,
  type ThesisBuilderThroughputResponse,
  type ThesisBuilderEvidenceWindow,
  type ThesisCardSummary,
  type ThesisReprocessStatusResponse,
  type ThroughputPresetWindow,
  type WindowArticle,
  type WindowArticlesResponse
} from "../api";

const REPROCESS_TERMINAL_STATES = new Set(["completed", "failed"]);

const WINDOW_PRESETS: Array<{ label: string; window: ThroughputPresetWindow }> = [
  { label: "15m", window: "15m" },
  { label: "1h", window: "1h" },
  { label: "1d", window: "1d" },
  { label: "7d", window: "7d" },
  { label: "30d", window: "30d" }
];

export function ThesisBuilderTab() {
  const [window, setWindow] = useState<ThroughputPresetWindow>("1d");
  const [daysBack, setDaysBack] = useState(7);
  const [runId, setRunId] = useState<string | null>(null);
  const metrics = useQuery({
    queryKey: ["thesis-builder", "metrics", window],
    queryFn: () => fetchThesisBuilderMetrics(window),
    refetchInterval: 15000
  });
  const throughput = useQuery({
    queryKey: ["thesis-builder", "throughput", window],
    queryFn: () => fetchThesisBuilderThroughput(window),
    refetchInterval: 15000
  });
  const config = useQuery({
    queryKey: ["thesis-builder", "config"],
    queryFn: fetchThesisBuilderConfig,
    staleTime: 60000
  });
  const reprocess = useMutation({
    mutationFn: () => reprocessThesisBuilder({ days_back: daysBack }),
    onSuccess: (data) => setRunId(data.run_id)
  });
  const reprocessStatus = useQuery({
    queryKey: ["thesis-builder", "reprocess", runId],
    queryFn: () => fetchReprocessStatus(runId as string),
    enabled: runId !== null,
    refetchInterval: (query) =>
      REPROCESS_TERMINAL_STATES.has(query.state.data?.status ?? "") ? false : 2000
  });
  const status = reprocessStatus.data;
  const isReprocessing =
    reprocess.isPending ||
    (runId !== null && status != null && !REPROCESS_TERMINAL_STATES.has(status.status));
  const data = metrics.data;
  const throughputData = throughput.data;
  const chartRows = aggregateThesisBuilderThroughputRows(throughputData);
  const throughputWindowStartAtMs = throughputData?.window_start_at
    ? new Date(throughputData.window_start_at).getTime()
    : undefined;
  const throughputWindowEndAtMs = throughputData?.window_end_at
    ? new Date(throughputData.window_end_at).getTime()
    : undefined;
  const throughputAxisDomain = getThesisBuilderThroughputAxisDomain(
    chartRows,
    throughputData?.granularity,
    window
  );
  const throughputAxisStartAtMs = throughputAxisDomain?.[0] ?? throughputWindowStartAtMs;
  const throughputAxisEndAtMs = throughputAxisDomain?.[1] ?? throughputWindowEndAtMs;
  const throughputAxisDurationMs =
    throughputAxisStartAtMs != null && throughputAxisEndAtMs != null
      ? Math.max(0, throughputAxisEndAtMs - throughputAxisStartAtMs)
      : undefined;
  const throughputUsesDiscretePoints = throughputData?.granularity === "raw" && window === "15m";
  const throughputTicks = buildThroughputAxisTicks(
    throughputData?.granularity,
    throughputAxisStartAtMs,
    throughputAxisEndAtMs
  );
  const throughputTickCount =
    throughputData?.granularity === "raw"
      ? throughputAxisDurationMs != null && throughputAxisDurationMs <= 3 * 24 * 60 * 60 * 1000
        ? 4
        : 6
      : undefined;

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
      <ConsumerHealthBanner health={data?.consumer_health} />
      <ThesisBuilderConfigPanel data={config.data} error={config.error} />

      <section className="panel panel-large">
        <div className="panel-heading">
          <div>
            <h2>Throughput</h2>
            <span>{formatThesisThroughputWindowSummary(throughputData, window)}</span>
          </div>
        </div>
        {throughputData && !throughputData.available ? (
          <div className="inline-error">{throughputData.message ?? "ThesisBuilder throughput unavailable."}</div>
        ) : null}
        <div className="consumer-stall-stats" style={{ paddingBottom: "0.75rem" }}>
          <span>Processed</span>
          <span>News catalyst</span>
          <span>Market moving</span>
        </div>
        <div className="chart">
          {chartRows.length > 0 ? (
            <ResponsiveContainer width="100%" height={290} minWidth={0}>
              <ComposedChart data={chartRows}>
                <CartesianGrid stroke="#dbe3dc" strokeDasharray="3 3" />
                <XAxis
                  dataKey="timestampMs"
                  type="number"
                  scale="time"
                  domain={[throughputAxisStartAtMs ?? "dataMin", throughputAxisEndAtMs ?? "dataMax"]}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={20}
                  tickCount={throughputTickCount}
                  ticks={throughputTicks}
                  tickFormatter={(value) =>
                    formatThroughputAxisTick(
                      Number(value),
                      throughputData?.granularity,
                      throughputAxisDurationMs
                    )
                  }
                />
                <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                <Tooltip content={<ThesisBuilderThroughputTooltip />} />
                {throughputUsesDiscretePoints ? (
                  <>
                    <Scatter dataKey="processed" fill="#4967d1" />
                    <Scatter dataKey="newsCatalyst" fill="#c47422" />
                    <Scatter dataKey="marketMoving" fill="#1d8f6f" />
                  </>
                ) : (
                  <>
                    <Bar dataKey="processed" name="Processed" fill="#4967d1" fillOpacity={0.72} radius={[3, 3, 0, 0]} barSize={12} />
                    <Bar dataKey="newsCatalyst" name="News catalyst" fill="#c47422" fillOpacity={0.8} radius={[3, 3, 0, 0]} barSize={12} />
                    <Bar dataKey="marketMoving" name="Market moving" fill="#1d8f6f" fillOpacity={0.82} radius={[3, 3, 0, 0]} barSize={12} />
                  </>
                )}
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState
              text={!throughputData?.available ? "ThesisBuilder throughput unavailable" : "No ThesisBuilder throughput yet"}
            />
          )}
        </div>
      </section>

      <section className="thesis-kpi-grid">
        <MetricTile label="Articles processed" value={formatInteger(data?.articles_processed_count)} />
        <MetricTile label="Market moving" value={formatInteger(data?.market_moving_articles_count)} />
        <MetricTile label="Included in cards" value={formatInteger(data?.articles_included_in_cards_count)} />
        <MetricTile label="Too old" value={formatInteger(data?.stale_articles_count)} />
        <MetricTile label="Created cards" value={formatInteger(data?.created_thesis_cards_count)} />
        <MetricTile label="Actionable cards" value={formatInteger(data?.actionable_cards?.length)} />
        <MetricTile label="Evidence windows" value={formatInteger(data?.pending_thesis_cards_count)} />
        <MetricTile label="Dead letters" value={formatInteger(data?.dead_letter_count)} />
        <MetricTile label="Oldest pending age" value={formatDuration(data?.oldest_pending_age_seconds)} />
        <MetricTile label="Avg pending age" value={formatDuration(data?.average_pending_age_seconds)} />
        <MetricTile label="Min time to expiry" value={formatDuration(data?.minimum_pending_expires_in_seconds)} />
        <MetricTile label="Avg time to expiry" value={formatDuration(data?.average_pending_expires_in_seconds)} />
      </section>

      <ThesisCardsPanel window={window} />
      <EvidenceWindowsPanel windows={data?.pending_windows ?? []} />
      <NewsAnalysesPanel window={window} />
      <section className="layout thesis-layout">
        <StaleEvidencePanel data={data} />
        <DeadLetterPanel data={data} />
      </section>

      <ReprocessPanel
        daysBack={daysBack}
        onDaysBackChange={setDaysBack}
        onReprocess={() => reprocess.mutate()}
        isPending={isReprocessing}
        status={status}
        error={reprocess.error ?? reprocessStatus.error}
      />
    </main>
  );
}

function ConsumerHealthBanner({ health }: { health?: ThesisBuilderConsumerHealth | null }) {
  if (!health || !health.available || !health.stalled) {
    return null;
  }
  return (
    <div className="inline-error consumer-stall-banner" role="alert">
      <strong>ThesisBuilder consumer stalled.</strong>
      <ul style={{ margin: "6px 0 8px", paddingLeft: "1.2rem" }}>
        {health.stall_reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <div className="consumer-stall-stats">
        <span>Backlog: {formatInteger((health.consumer_lag ?? 0) + (health.pending_count ?? 0))}</span>
        <span>Stream depth: {formatInteger(health.stream_length)}</span>
        <span>Consumers: {formatInteger(health.consumer_count)}</span>
        <span>Last analysis: {formatDuration(health.last_analysis_age_seconds)} ago</span>
      </div>
    </div>
  );
}

function ThesisBuilderConfigPanel({ data, error }: { data?: ThesisBuilderConfigResponse; error: Error | null }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Already-Priced Gate</h2>
          <span>ThesisBuilder-owned thresholds</span>
        </div>
      </div>
      {error && <div className="inline-error">{error.message}</div>}
      <div className="quality-grid thesis-stale-grid">
        {(data?.already_priced_gate ?? []).map((gate) => (
          <div className="quality-value" key={gate.strategy}>
            <span>{formatToken(gate.strategy)}</span>
            <strong>{gate.atr_multiple.toFixed(1)} ATR / {formatPercent(gate.return_threshold)}</strong>
          </div>
        ))}
        {!data && !error ? <EmptyState text="Loading gate config" /> : null}
      </div>
    </section>
  );
}

function EvidenceWindowsPanel({ windows }: { windows: ThesisBuilderEvidenceWindow[] }) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selectedWindow = windows.find((w) => w.window_id === selectedId) ?? null;

  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Evidence windows</h2>
          <span>Collecting evidence windows</span>
        </div>
      </div>
      {windows.length === 0 ? (
        <div className="empty">No evidence windows.</div>
      ) : (
        <div className="pending-windows-layout">
          <div className="pending-list-wrap">
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
                {windows.map((w) => (
                  <tr
                    key={w.window_id}
                    data-selectable
                    className={w.window_id === selectedId ? "selected" : undefined}
                    onClick={() => setSelectedId(w.window_id)}
                  >
                    <td>
                      <strong>{w.ticker}</strong>
                      <span className="table-subtext">{w.exchange_code}</span>
                    </td>
                    <td>{formatToken(w.strategy)}</td>
                    <td>{w.direction ? formatToken(w.direction) : "n/a"}</td>
                    <td>{formatDuration(w.pending_age_seconds)}</td>
                    <td>{formatDuration(w.expires_in_seconds)}</td>
                    <td>{formatDate(w.last_evidence_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pending-detail">
            {selectedWindow ? (
              <WindowDetail window={selectedWindow} />
            ) : (
              <div className="empty">Select a card to see details.</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

type AnalysisStatusFilter = "all" | "valid" | "rejected";
type AnalysisTypeFilter = "all" | "news_catalyst" | "opinion";

function NewsAnalysesPanel({ window }: { window: ThroughputPresetWindow }) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [tickerFilter, setTickerFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<AnalysisStatusFilter>("all");
  const [typeFilter, setTypeFilter] = useState<AnalysisTypeFilter>("all");

  const query = useQuery({
    queryKey: ["thesis-builder", "analyses", window],
    queryFn: () => fetchNewsAnalyses({ window, limit: 200 }),
    refetchInterval: 15000,
  });

  const allItems = query.data?.items ?? [];
  const visible = allItems.filter((item) => {
    if (tickerFilter && !item.ticker.toLowerCase().includes(tickerFilter.toLowerCase())) return false;
    if (statusFilter !== "all" && item.validation_status !== statusFilter) return false;
    if (typeFilter !== "all" && item.content_type !== typeFilter) return false;
    return true;
  });
  const selectedItem = allItems.find((item) => item.id === selectedId) ?? null;

  return (
    <section className="panel panel-large" style={{ marginBottom: 20 }}>
      <div className="panel-heading">
        <div>
          <h2>LLM Analyses</h2>
          <span>Recent analyses from the thesis builder, newest first</span>
        </div>
      </div>
      <div className="thesis-card-filters">
        <label>
          Ticker
          <input
            type="text"
            aria-label="Filter analyses by ticker"
            value={tickerFilter}
            placeholder="e.g. AAPL"
            onChange={(e) => setTickerFilter(e.target.value)}
          />
        </label>
        <label>
          Status
          <select
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as AnalysisStatusFilter)}
          >
            <option value="all">All</option>
            <option value="valid">Valid</option>
            <option value="rejected">Rejected</option>
          </select>
        </label>
        <label>
          Type
          <select
            aria-label="Filter by content type"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value as AnalysisTypeFilter)}
          >
            <option value="all">All</option>
            <option value="news_catalyst">News catalyst</option>
            <option value="opinion">Opinion</option>
          </select>
        </label>
      </div>
      {query.isError && <div className="inline-error">{query.error.message}</div>}
      {query.data && !query.data.available && (
        <div className="inline-error">{query.data.message ?? "Analyses unavailable."}</div>
      )}
      {visible.length === 0 ? (
        <div className="empty">{query.isLoading ? "Loading analyses…" : "No analyses match the current filters."}</div>
      ) : (
        <div className="pending-windows-layout">
          <div className="pending-list-wrap analyses-list-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Instrument</th>
                  <th>Type</th>
                  <th>Status / Reason</th>
                  <th>Dir</th>
                  <th>Conf</th>
                  <th>Headline</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item) => (
                  <tr
                    key={item.id}
                    data-selectable
                    className={item.id === selectedId ? "selected" : undefined}
                    onClick={() => setSelectedId(item.id === selectedId ? null : item.id)}
                  >
                    <td style={{ whiteSpace: "nowrap" }}>{formatDate(item.analyzed_at)}</td>
                    <td>
                      <strong>{item.ticker}</strong>
                      <span className="table-subtext">{item.exchange_code}</span>
                    </td>
                    <td>
                      {item.content_type ? (
                        <span className={item.content_type === "news_catalyst" ? "chip" : "chip warning"}>
                          {formatToken(item.content_type)}
                        </span>
                      ) : (
                        <span style={{ color: "#64726c" }}>—</span>
                      )}
                    </td>
                    <td>
                      {item.validation_status === "valid" ? (
                        <span className="chip">valid</span>
                      ) : (
                        <span className="chip warning">{formatToken(item.rejection_reason_code ?? "rejected")}</span>
                      )}
                    </td>
                    <td>{item.direction ? formatToken(item.direction) : "—"}</td>
                    <td>{item.confidence.toFixed(2)}</td>
                    <td className="analysis-headline-cell">
                      {item.url ? (
                        <a href={item.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                          {item.headline ?? item.article_id}
                        </a>
                      ) : (
                        <span>{item.headline ?? item.article_id}</span>
                      )}
                      {item.source && <span className="table-subtext">{item.source}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {query.data?.has_more && (
              <div style={{ padding: "10px 14px", color: "#64726c", fontSize: "0.85rem" }}>
                Showing 200 most recent — narrow the window to see fewer results.
              </div>
            )}
          </div>
          <div className="pending-detail">
            {selectedItem ? (
              <AnalysisDetail item={selectedItem} />
            ) : (
              <div className="empty">Select a row to see reasoning.</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function AnalysisDetail({ item }: { item: NewsAnalysisItem }) {
  return (
    <div className="pending-detail-grid">
      <div className="pending-detail-row">
        <span>Analysis ID</span>
        <strong style={{ fontSize: "0.82rem", fontVariantNumeric: "tabular-nums" }}>{item.id}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Article ID</span>
        <strong style={{ fontSize: "0.82rem", wordBreak: "break-all" }}>{item.article_id}</strong>
      </div>
      {item.headline && (
        <div className="pending-detail-row analysis-reasoning">
          <span>Title</span>
          {item.url ? (
            <a href={item.url} target="_blank" rel="noreferrer" className="quality-link">
              {item.headline}
            </a>
          ) : (
            <p>{item.headline}</p>
          )}
        </div>
      )}
      {item.summary && (
        <div className="pending-detail-row analysis-reasoning">
          <span>Summary</span>
          <p>{item.summary}</p>
        </div>
      )}
      {item.url && (
        <div className="pending-detail-row analysis-reasoning">
          <span>URL</span>
          <a href={item.url} target="_blank" rel="noreferrer" className="quality-link" style={{ fontSize: "0.82rem" }}>
            {item.url}
          </a>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Instrument</span>
        <strong>{item.ticker} <small style={{ fontWeight: 400, color: "#64726c" }}>{item.exchange_code}</small></strong>
      </div>
      <div className="pending-detail-row">
        <span>Analyzed at</span>
        <strong>{formatDate(item.analyzed_at)}</strong>
      </div>
      {item.content_type && (
        <div className="pending-detail-row">
          <span>Content type</span>
          <strong>{formatToken(item.content_type)}</strong>
        </div>
      )}
      {item.subject_relation && (
        <div className="pending-detail-row">
          <span>Subject relation</span>
          <strong>{formatToken(item.subject_relation)}</strong>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Status</span>
        <strong>{formatToken(item.validation_status)}</strong>
      </div>
      {item.rejection_reason_code && (
        <div className="pending-detail-row">
          <span>Rejection reason</span>
          <strong>{formatToken(item.rejection_reason_code)}</strong>
        </div>
      )}
      {item.strategy && (
        <div className="pending-detail-row">
          <span>Strategy</span>
          <strong>{formatToken(item.strategy)}</strong>
        </div>
      )}
      {item.direction && (
        <div className="pending-detail-row">
          <span>Direction</span>
          <strong>{formatToken(item.direction)}</strong>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Confidence</span>
        <strong>{item.confidence.toFixed(2)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Market moving</span>
        <strong>{item.is_market_moving ? "yes" : "no"}</strong>
      </div>
      {item.reasoning && (
        <div className="pending-detail-row analysis-reasoning">
          <span>Reasoning</span>
          <p>{item.reasoning}</p>
        </div>
      )}
    </div>
  );
}

function WindowDetail({ window: w }: { window: ThesisBuilderEvidenceWindow }) {
  const [articlesOpen, setArticlesOpen] = useState(false);
  return (
    <div className="pending-detail-grid">
      <div className="pending-detail-row">
        <span>Instrument</span>
        <strong>{w.ticker} <small style={{ fontWeight: 400, color: "#64726c" }}>{w.exchange_code}</small></strong>
      </div>
      <div className="pending-detail-row">
        <span>Strategy</span>
        <strong>{formatToken(w.strategy)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Direction</span>
        <strong>{w.direction ? formatToken(w.direction) : "n/a"}</strong>
      </div>
      {w.story_narrative && (
        <div className="pending-detail-row analysis-reasoning">
          <span>Story</span>
          <strong>{w.story_narrative}</strong>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Window started</span>
        <strong>{formatDate(w.window_started_at)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Age</span>
        <strong>{formatDuration(w.pending_age_seconds)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Expires in</span>
        <strong>{formatDuration(w.expires_in_seconds)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Last evidence</span>
        <strong>{formatDate(w.last_evidence_at)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Evidence</span>
        <strong>{w.evidence_count} of {w.required_evidence_count} articles</strong>
      </div>
      <button
        type="button"
        className="quality-link"
        onClick={() => setArticlesOpen(true)}
        disabled={w.evidence_count === 0}
      >
        View {w.evidence_count} evidence article{w.evidence_count === 1 ? "" : "s"} →
      </button>
      {articlesOpen && (
        <EvidenceArticlesModal
          title={`Evidence articles · ${w.ticker} ${w.exchange_code}`}
          queryKey={["thesis-window-articles", w.window_id]}
          queryFn={() => fetchWindowArticles(w.window_id)}
          onClose={() => setArticlesOpen(false)}
        />
      )}
    </div>
  );
}

function EvidenceArticlesModal({
  title,
  queryKey,
  queryFn,
  onClose,
}: {
  title: string;
  queryKey: (string | number)[];
  queryFn: () => Promise<WindowArticlesResponse>;
  onClose: () => void;
}) {
  const articles = useQuery({
    queryKey,
    queryFn
  });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const items = articles.data?.articles ?? [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Evidence articles"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <h3>{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-body">
          {articles.isLoading && <div className="empty">Loading articles…</div>}
          {articles.isError && <div className="inline-error">{articles.error.message}</div>}
          {!articles.isLoading && !articles.isError && items.length === 0 && (
            <div className="empty">No articles recorded for this window.</div>
          )}
          {items.length > 0 && (
            <div className="evidence-article-list">
              {items.map((article) => (
                <EvidenceArticle key={article.article_id} article={article} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function EvidenceArticle({ article }: { article: WindowArticle }) {
  return (
    <div className="evidence-article">
      {article.url ? (
        <a href={article.url} target="_blank" rel="noreferrer" className="evidence-article-headline">
          {article.headline || article.url}
        </a>
      ) : (
        <span className="evidence-article-headline">{article.headline || "Untitled article"}</span>
      )}
      <div className="evidence-article-meta">
        <span>{article.source || "unknown source"}</span>
        <span>{formatDate(article.published_at)}</span>
      </div>
      <div className="evidence-article-badges">
        {article.confidence != null && (
          <span className="chip">conf {article.confidence.toFixed(2)}</span>
        )}
        {article.is_market_moving && <span className="chip">market-moving</span>}
        {article.validation_status && (
          <span className={article.validation_status === "rejected" ? "chip warning" : "chip"}>
            {article.rejection_reason_code
              ? formatToken(article.rejection_reason_code)
              : formatToken(article.validation_status)}
          </span>
        )}
      </div>
      {article.summary && <p className="evidence-article-summary">{article.summary}</p>}
    </div>
  );
}

type ValidationFilter = "all" | "valid" | "rejected";
type DirectionFilter = "buy_sell" | "all" | "buy" | "sell" | "hold";
type SignalFilter = "all" | "published" | "pending";

function matchesFilters(
  card: ThesisCardSummary,
  filters: {
    validation: ValidationFilter;
    direction: DirectionFilter;
    signal: SignalFilter;
    strategy: string;
    ticker: string;
    showExpired: boolean;
  }
): boolean {
  if (filters.validation !== "all" && card.validation_status !== filters.validation) {
    return false;
  }
  if (filters.direction === "buy_sell") {
    if (card.direction !== "buy" && card.direction !== "sell") {
      return false;
    }
  } else if (filters.direction !== "all" && card.direction !== filters.direction) {
    return false;
  }
  if (filters.signal === "published" && !card.signal_published) {
    return false;
  }
  if (filters.signal === "pending" && card.signal_published) {
    return false;
  }
  if (filters.strategy !== "all" && card.strategy !== filters.strategy) {
    return false;
  }
  if (filters.ticker && !card.ticker.toLowerCase().includes(filters.ticker.toLowerCase())) {
    return false;
  }
  if (!filters.showExpired && card.expires_in_seconds <= 0) {
    return false;
  }
  return true;
}

function ThesisCardsPanel({ window }: { window: ThroughputPresetWindow }) {
  const cardsQuery = useQuery({
    queryKey: ["thesis-builder", "cards", window],
    queryFn: () => fetchThesisCards(window),
    refetchInterval: 15000
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [validation, setValidation] = useState<ValidationFilter>("valid");
  const [direction, setDirection] = useState<DirectionFilter>("buy_sell");
  const [signal, setSignal] = useState<SignalFilter>("all");
  const [strategy, setStrategy] = useState<string>("all");
  const [ticker, setTicker] = useState<string>("");
  const [showExpired, setShowExpired] = useState(false);

  const cards = cardsQuery.data?.cards ?? [];
  const strategyOptions = Array.from(new Set(cards.map((c) => c.strategy))).sort();
  const filters = { validation, direction, signal, strategy, ticker, showExpired };
  const visible = cards.filter((c) => matchesFilters(c, filters));
  const selectedCard = visible.find((c) => c.card_id === selectedId) ?? null;

  const emptyText =
    cards.length === 0
      ? "No thesis cards in this window."
      : "No cards match the current filters.";

  return (
    <section className="panel panel-large">
      <div className="panel-heading">
        <div>
          <h2>Thesis Cards</h2>
          <span>All thesis cards in the window, newest first — filter to slice</span>
        </div>
      </div>
      <div className="thesis-card-filters">
        <label>
          Status
          <select
            aria-label="Filter by validation status"
            value={validation}
            onChange={(e) => setValidation(e.target.value as ValidationFilter)}
          >
            <option value="all">All</option>
            <option value="valid">Valid</option>
            <option value="rejected">Rejected</option>
          </select>
        </label>
        <label>
          Direction
          <select
            aria-label="Filter by direction"
            value={direction}
            onChange={(e) => setDirection(e.target.value as DirectionFilter)}
          >
            <option value="buy_sell">Buy + Sell</option>
            <option value="all">All</option>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
            <option value="hold">Hold</option>
          </select>
        </label>
        <label>
          Signal
          <select
            aria-label="Filter by signal"
            value={signal}
            onChange={(e) => setSignal(e.target.value as SignalFilter)}
          >
            <option value="all">All</option>
            <option value="published">Published</option>
            <option value="pending">Pending</option>
          </select>
        </label>
        <label>
          Strategy
          <select
            aria-label="Filter by strategy"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
          >
            <option value="all">All</option>
            {strategyOptions.map((s) => (
              <option key={s} value={s}>{formatToken(s)}</option>
            ))}
          </select>
        </label>
        <label>
          Ticker
          <input
            type="text"
            aria-label="Filter by ticker"
            value={ticker}
            placeholder="e.g. AAPL"
            onChange={(e) => setTicker(e.target.value)}
          />
        </label>
        <label className="thesis-card-filter-check">
          <input
            type="checkbox"
            checked={showExpired}
            onChange={(e) => setShowExpired(e.target.checked)}
          />
          Show expired
        </label>
      </div>
      {cardsQuery.isError && <div className="inline-error">{cardsQuery.error.message}</div>}
      {cardsQuery.data && !cardsQuery.data.available && (
        <div className="inline-error">{cardsQuery.data.message ?? "Thesis cards unavailable."}</div>
      )}
      {visible.length === 0 ? (
        <div className="empty">{cardsQuery.isLoading ? "Loading thesis cards…" : emptyText}</div>
      ) : (
        <div className="pending-windows-layout">
          <div className="pending-list-wrap">
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Strategy</th>
                  <th>Direction</th>
                  <th>Confidence</th>
                  <th>Status</th>
                  <th>Expires in</th>
                  <th>Corrob.</th>
                  <th>Created</th>
                  <th>Signal</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((c) => (
                  <tr
                    key={c.card_id}
                    data-selectable
                    className={[
                      c.card_id === selectedId ? "selected" : "",
                      c.expires_in_seconds <= 0 ? "expired-row" : "",
                    ].filter(Boolean).join(" ") || undefined}
                    onClick={() => setSelectedId(c.card_id)}
                  >
                    <td>
                      <strong>{c.ticker}</strong>
                      <span className="table-subtext">{c.exchange_code}</span>
                    </td>
                    <td>{formatToken(c.strategy)}</td>
                    <td>{formatToken(c.direction)}</td>
                    <td>{c.confidence.toFixed(2)}</td>
                    <td>
                      <span className={c.validation_status === "valid" ? "chip" : "chip warning"}>
                        {formatToken(c.validation_status)}
                      </span>
                    </td>
                    <td>{formatExpiresIn(c.expires_in_seconds)}</td>
                    <td>{c.corroboration_count}</td>
                    <td>{formatDate(c.created_at)}</td>
                    <td>
                      <span className={c.signal_published ? "chip" : "chip warning"}>
                        {c.signal_published ? "published" : "pending"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pending-detail">
            {selectedCard ? (
              <ThesisCardDetail card={selectedCard} />
            ) : (
              <div className="empty">Select a card to see details.</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function ThesisCardDetail({ card: c }: { card: ThesisCardSummary }) {
  const [articlesOpen, setArticlesOpen] = useState(false);
  return (
    <div className="pending-detail-grid">
      <div className="pending-detail-row">
        <span>Card ID</span>
        <strong style={{ fontSize: "0.8rem", wordBreak: "break-all" }}>{c.card_id}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Instrument</span>
        <strong>{c.ticker} <small style={{ fontWeight: 400, color: "#64726c" }}>{c.exchange_code}</small></strong>
      </div>
      <div className="pending-detail-row">
        <span>Strategy</span>
        <strong>{formatToken(c.strategy)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Direction</span>
        <strong>{formatToken(c.direction)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Time horizon</span>
        <strong>{formatToken(c.time_horizon)}</strong>
      </div>
      {c.story_narrative && (
        <div className="pending-detail-row analysis-reasoning">
          <span>Story</span>
          <strong>{c.story_narrative}</strong>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Confidence</span>
        <strong>{c.confidence.toFixed(2)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Validation status</span>
        <strong>{formatToken(c.validation_status)}</strong>
      </div>
      {c.validation_status !== "valid" && c.rejection_reason_code && (
        <div className="pending-detail-row">
          <span>Rejection reason</span>
          <strong>{formatToken(c.rejection_reason_code)}</strong>
        </div>
      )}
      <div className="pending-detail-row">
        <span>Created</span>
        <strong>{formatDate(c.created_at)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Expires in</span>
        <strong>{formatExpiresIn(c.expires_in_seconds)}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Evidence</span>
        <strong>{c.evidence_count} article{c.evidence_count === 1 ? "" : "s"}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Corroborations</span>
        <strong>{c.corroboration_count} article{c.corroboration_count === 1 ? "" : "s"}</strong>
      </div>
      <div className="pending-detail-row">
        <span>Signal</span>
        <strong>{c.signal_published ? "published" : "pending"}</strong>
      </div>
      <button
        type="button"
        className="quality-link"
        onClick={() => setArticlesOpen(true)}
        disabled={c.evidence_count === 0}
      >
        View {c.evidence_count} evidence article{c.evidence_count === 1 ? "" : "s"} →
      </button>
      {articlesOpen && (
        <EvidenceArticlesModal
          title={`Evidence articles · ${c.ticker} ${c.exchange_code}`}
          queryKey={["thesis-card-articles", c.card_id]}
          queryFn={() => fetchThesisCardArticles(c.card_id)}
          onClose={() => setArticlesOpen(false)}
        />
      )}
    </div>
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

function ReprocessPanel({
  daysBack,
  onDaysBackChange,
  onReprocess,
  isPending,
  status,
  error,
}: {
  daysBack: number;
  onDaysBackChange: (v: number) => void;
  onReprocess: () => void;
  isPending: boolean;
  status?: ThesisReprocessStatusResponse;
  error: Error | null;
}) {
  const isComplete = status?.status === "completed";
  const isFailed = status?.status === "failed";
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Reprocess Historical Articles</h2>
          <span>Testing only — will not overwrite existing thesis cards</span>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", padding: "0.75rem 0" }}>
        <label htmlFor="reprocess-days" style={{ whiteSpace: "nowrap" }}>
          Days back
        </label>
        <input
          id="reprocess-days"
          type="number"
          min={1}
          max={30}
          value={daysBack}
          onChange={(e) => onDaysBackChange(Math.max(1, Math.min(30, parseInt(e.target.value, 10) || 1)))}
          style={{ width: "5rem" }}
          disabled={isPending}
        />
        <button type="button" onClick={onReprocess} disabled={isPending}>
          {isPending ? "Reprocessing…" : "Reprocess"}
        </button>
        {isPending && (
          <span style={{ color: "var(--color-muted, #888)", fontSize: "0.85em" }}>
            Running in ThesisBuilder — this may take several minutes
          </span>
        )}
      </div>
      {error && <div className="inline-error">{error.message}</div>}
      {isFailed && (
        <div className="inline-error">Reprocess run failed{status?.error_code ? `: ${status.error_code}` : ""}</div>
      )}
      {status && (
        <div className="quality-grid thesis-stale-grid" style={{ marginTop: "0.5rem" }}>
          <QualityValue label="Run ID" value={status.run_id.slice(0, 8) + "…"} />
          <QualityValue label="Status" value={status.status} />
          <QualityValue label="Articles found" value={isComplete ? String(status.articles_found ?? 0) : "—"} />
          <QualityValue label="Analyses created" value={isComplete ? String(status.analyses_created ?? 0) : "—"} />
          <QualityValue label="Cards created" value={isComplete ? String(status.cards_created ?? 0) : "—"} />
        </div>
      )}
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

function ThesisBuilderThroughputTooltip({
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
      label: formatThesisThroughputSeriesLabel(String(entry.dataKey)),
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

function formatInteger(value?: number | null) {
  return value == null ? "n/a" : value.toLocaleString();
}

function formatPercent(value?: number | null) {
  return value == null ? "n/a" : `${(value * 100).toFixed(1)}%`;
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

function formatExpiresIn(seconds?: number | null) {
  if (seconds == null) {
    return "n/a";
  }
  if (seconds <= 0) {
    return `Expired ${formatDuration(Math.abs(seconds))} ago`;
  }
  return formatDuration(seconds);
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

function formatThesisThroughputWindowSummary(
  data: ThesisBuilderThroughputResponse | undefined,
  window: ThroughputPresetWindow
) {
  if (!data) {
    return `Last ${window}`;
  }
  return `${formatDate(data.window_start_at)} to ${formatDate(data.window_end_at)}`;
}

function formatThroughputAxisTick(
  value: number,
  granularity?: ThesisBuilderThroughputResponse["granularity"],
  windowDurationMs?: number
) {
  const date = new Date(value);
  if (granularity === "day") {
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  if (granularity === "hour") {
    if (windowDurationMs != null && windowDurationMs <= 24 * 60 * 60 * 1000) {
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }
  if (windowDurationMs != null && windowDurationMs <= 24 * 60 * 60 * 1000) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (windowDurationMs != null && windowDurationMs <= 3 * 24 * 60 * 60 * 1000) {
    return date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function buildThroughputAxisTicks(
  granularity?: ThesisBuilderThroughputResponse["granularity"],
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

function formatThesisThroughputSeriesLabel(value: string) {
  if (value === "processed") {
    return "Processed";
  }
  if (value === "newsCatalyst") {
    return "News catalyst";
  }
  if (value === "marketMoving") {
    return "Market moving";
  }
  return value;
}

function aggregateThesisBuilderThroughputRows(response?: ThesisBuilderThroughputResponse) {
  if (!response) {
    return [];
  }
  return response.buckets
    .map((bucket) => ({
      timestamp: bucket.window_start,
      timestampMs: new Date(bucket.window_start).getTime(),
      processed: bucket.processed_articles_count,
      newsCatalyst: bucket.news_catalyst_articles_count,
      marketMoving: bucket.market_moving_articles_count
    }))
    .sort((left, right) => left.timestampMs - right.timestampMs);
}

type ThesisBuilderThroughputChartRow = ReturnType<typeof aggregateThesisBuilderThroughputRows>[number];

function getThesisBuilderThroughputAxisDomain(
  chartRows: ThesisBuilderThroughputChartRow[],
  granularity: ThesisBuilderThroughputResponse["granularity"] | undefined,
  window: ThroughputPresetWindow
): [number, number] | undefined {
  const dataStartAtMs = chartRows[0]?.timestampMs;
  const dataEndAtMs = chartRows[chartRows.length - 1]?.timestampMs;
  if (dataStartAtMs == null || dataEndAtMs == null) {
    return undefined;
  }
  if (dataStartAtMs !== dataEndAtMs) {
    return [dataStartAtMs, dataEndAtMs];
  }
  const paddingMs = thesisBuilderThroughputBucketPaddingMs(granularity, window);
  return [dataStartAtMs - paddingMs, dataEndAtMs + paddingMs];
}

function thesisBuilderThroughputBucketPaddingMs(
  granularity: ThesisBuilderThroughputResponse["granularity"] | undefined,
  window: ThroughputPresetWindow
) {
  if (granularity === "day") {
    return 12 * 60 * 60 * 1000;
  }
  if (granularity === "hour") {
    return 30 * 60 * 1000;
  }
  return window === "15m" ? 60 * 1000 : 5 * 60 * 1000;
}
