import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  fetchThesisBuilderThroughput,
  fetchThroughput,
  type ThesisBuilderThroughputResponse,
  type ThroughputPresetWindow,
  type ThroughputResponse
} from "../api";

const WINDOW_PRESETS: Array<{ label: string; window: ThroughputPresetWindow }> = [
  { label: "15m", window: "15m" },
  { label: "1h", window: "1h" },
  { label: "1d", window: "1d" },
  { label: "7d", window: "7d" },
  { label: "30d", window: "30d" }
];

type OverviewThroughputRow = {
  timestamp: string;
  timestampMs: number;
  published: number;
  processed: number;
};

export function OverviewTab() {
  const [window, setWindow] = useState<ThroughputPresetWindow>("1d");
  const newsFetcherThroughput = useQuery({
    queryKey: ["overview", "news-fetcher-throughput", window],
    queryFn: () => fetchThroughput({ window }),
    refetchInterval: 15000
  });
  const thesisBuilderThroughput = useQuery({
    queryKey: ["overview", "thesis-builder-throughput", window],
    queryFn: () => fetchThesisBuilderThroughput(window),
    refetchInterval: 15000
  });

  const chartRows = aggregateOverviewThroughputRows(
    newsFetcherThroughput.data,
    thesisBuilderThroughput.data
  );
  const axisStartAtMs = minDefined([
    toTimestampMs(newsFetcherThroughput.data?.window_start_at),
    toTimestampMs(thesisBuilderThroughput.data?.window_start_at),
    chartRows[0]?.timestampMs
  ]);
  const axisEndAtMs = maxDefined([
    toTimestampMs(newsFetcherThroughput.data?.window_end_at),
    toTimestampMs(thesisBuilderThroughput.data?.window_end_at),
    chartRows[chartRows.length - 1]?.timestampMs
  ]);
  const axisDurationMs =
    axisStartAtMs != null && axisEndAtMs != null ? Math.max(0, axisEndAtMs - axisStartAtMs) : undefined;
  const granularity = thesisBuilderThroughput.data?.granularity ?? newsFetcherThroughput.data?.granularity;
  const ticks = buildThroughputAxisTicks(granularity, axisStartAtMs, axisEndAtMs);
  const tickCount =
    granularity === "raw"
      ? axisDurationMs != null && axisDurationMs <= 3 * 24 * 60 * 60 * 1000
        ? 4
        : 6
      : undefined;
  const publishedTotal = chartRows.reduce((total, row) => total + row.published, 0);
  const processedTotal = chartRows.reduce((total, row) => total + row.processed, 0);
  const lastGeneratedAt = maxIso([
    newsFetcherThroughput.data?.generated_at,
    thesisBuilderThroughput.data?.generated_at
  ]);

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Trader</p>
          <h1>Overview</h1>
        </div>
        <div className="timestamp">
          Last refresh
          <strong>{formatDate(lastGeneratedAt)}</strong>
        </div>
      </header>

      <section className="panel thesis-window-panel">
        <div className="panel-heading">
          <div>
            <h2>Window</h2>
            <span>{formatWindowSummary(newsFetcherThroughput.data, thesisBuilderThroughput.data, window)}</span>
          </div>
        </div>
        <div className="window-toggle-group" aria-label="Overview time window">
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

      <section className="status-grid" aria-label="Pipeline totals">
        <MetricTile label="Published to queue" value={formatInteger(publishedTotal)} />
        <MetricTile label="Processed by ThesisBuilder" value={formatInteger(processedTotal)} />
        <MetricTile label="Unprocessed delta" value={formatInteger(Math.max(0, publishedTotal - processedTotal))} />
        <MetricTile label="Buckets with activity" value={formatInteger(chartRows.length)} />
      </section>

      {newsFetcherThroughput.data && !newsFetcherThroughput.data.available ? (
        <div className="inline-error">{newsFetcherThroughput.data.message ?? "NewsFetcher throughput unavailable."}</div>
      ) : null}
      {thesisBuilderThroughput.data && !thesisBuilderThroughput.data.available ? (
        <div className="inline-error">
          {thesisBuilderThroughput.data.message ?? "ThesisBuilder throughput unavailable."}
        </div>
      ) : null}
      {newsFetcherThroughput.isError ? <div className="inline-error">{newsFetcherThroughput.error.message}</div> : null}
      {thesisBuilderThroughput.isError ? (
        <div className="inline-error">{thesisBuilderThroughput.error.message}</div>
      ) : null}

      <section className="panel panel-large">
        <div className="panel-heading">
          <div>
            <h2>Pipeline Throughput</h2>
            <span>{formatWindowSummary(newsFetcherThroughput.data, thesisBuilderThroughput.data, window)}</span>
          </div>
        </div>
        <div className="overview-legend" aria-label="Throughput series">
          <span><i className="legend-swatch published" />Published to queue</span>
          <span><i className="legend-swatch processed" />Processed by ThesisBuilder</span>
        </div>
        <div className="chart">
          {chartRows.length > 0 ? (
            <ResponsiveContainer width="100%" height={320} minWidth={0}>
              <ComposedChart data={chartRows}>
                <CartesianGrid stroke="#dbe3dc" strokeDasharray="3 3" />
                <XAxis
                  dataKey="timestampMs"
                  type="number"
                  scale="time"
                  domain={[axisStartAtMs ?? "dataMin", axisEndAtMs ?? "dataMax"]}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={20}
                  tickCount={tickCount}
                  ticks={ticks}
                  tickFormatter={(value) => formatThroughputAxisTick(Number(value), granularity, axisDurationMs)}
                />
                <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                <Tooltip content={<OverviewThroughputTooltip />} />
                <Bar dataKey="published" name="Published to queue" fill="#1d8f6f" fillOpacity={0.84} radius={[3, 3, 0, 0]} />
                <Bar dataKey="processed" name="Processed by ThesisBuilder" fill="#4967d1" fillOpacity={0.74} radius={[3, 3, 0, 0]} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState
              text={
                newsFetcherThroughput.data?.available === false || thesisBuilderThroughput.data?.available === false
                  ? "Pipeline throughput unavailable"
                  : "No pipeline throughput yet"
              }
            />
          )}
        </div>
      </section>
    </main>
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

function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function OverviewThroughputTooltip({
  active,
  label,
  payload
}: {
  active?: boolean;
  label?: string | number;
  payload?: Array<{ name?: string; dataKey?: string; value?: number | string | null }>;
}) {
  if (!active || label == null || !payload?.length) {
    return null;
  }
  const items = payload.filter((entry) => entry.dataKey && entry.dataKey !== "timestampMs" && entry.value != null);
  if (!items.length) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <strong>{formatThroughputTooltipLabel(Number(label))}</strong>
      {items.map((item) => (
        <div key={item.dataKey}>
          {item.name ?? item.dataKey}: {item.value}
        </div>
      ))}
    </div>
  );
}

function aggregateOverviewThroughputRows(
  newsFetcherData?: ThroughputResponse,
  thesisBuilderData?: ThesisBuilderThroughputResponse
): OverviewThroughputRow[] {
  const totals = new Map<string, OverviewThroughputRow>();
  for (const bucket of newsFetcherData?.buckets ?? []) {
    const current = getOrCreateRow(totals, bucket.window_start);
    current.published += bucket.publish_success_count;
  }
  for (const bucket of thesisBuilderData?.buckets ?? []) {
    const current = getOrCreateRow(totals, bucket.window_start);
    current.processed += bucket.processed_articles_count;
  }
  return Array.from(totals.values())
    .filter((row) => row.published > 0 || row.processed > 0)
    .sort((left, right) => left.timestampMs - right.timestampMs);
}

function getOrCreateRow(totals: Map<string, OverviewThroughputRow>, timestamp: string) {
  const current = totals.get(timestamp);
  if (current) {
    return current;
  }
  const row = {
    timestamp,
    timestampMs: new Date(timestamp).getTime(),
    published: 0,
    processed: 0
  };
  totals.set(timestamp, row);
  return row;
}

function formatWindowSummary(
  newsFetcherData: ThroughputResponse | undefined,
  thesisBuilderData: ThesisBuilderThroughputResponse | undefined,
  window: ThroughputPresetWindow
) {
  const startAt = minIso([newsFetcherData?.window_start_at, thesisBuilderData?.window_start_at]);
  const endAt = maxIso([newsFetcherData?.window_end_at, thesisBuilderData?.window_end_at]);
  if (!startAt || !endAt) {
    return `Last ${window}`;
  }
  return `${formatDate(startAt)} to ${formatDate(endAt)}`;
}

function formatThroughputAxisTick(
  value: number,
  granularity?: ThroughputResponse["granularity"],
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
    const alignedHour = Math.ceil(cursor.getHours() / step) * step;
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

function formatInteger(value: number) {
  return value.toLocaleString();
}

function toTimestampMs(value?: string) {
  return value ? new Date(value).getTime() : undefined;
}

function minDefined(values: Array<number | undefined>) {
  const defined = values.filter((value): value is number => value != null);
  return defined.length ? Math.min(...defined) : undefined;
}

function maxDefined(values: Array<number | undefined>) {
  const defined = values.filter((value): value is number => value != null);
  return defined.length ? Math.max(...defined) : undefined;
}

function minIso(values: Array<string | undefined>) {
  const defined = values.filter((value): value is string => Boolean(value));
  if (!defined.length) {
    return undefined;
  }
  return defined.reduce((earliest, value) => (new Date(value) < new Date(earliest) ? value : earliest));
}

function maxIso(values: Array<string | undefined>) {
  const defined = values.filter((value): value is string => Boolean(value));
  if (!defined.length) {
    return undefined;
  }
  return defined.reduce((latest, value) => (new Date(value) > new Date(latest) ? value : latest));
}
