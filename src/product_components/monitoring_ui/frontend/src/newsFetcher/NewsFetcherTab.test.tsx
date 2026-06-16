import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "../api";
import { NewsFetcherTab } from "./NewsFetcherTab";

const fetchHealth = vi.fn();
const fetchProviders = vi.fn();
const fetchThroughput = vi.fn();
const fetchBacklog = vi.fn();
const fetchDeadLetters = vi.fn();
const fetchFilterQualityStatus = vi.fn();
const fetchFilterQualityIncorrectlyRejected = vi.fn();
const fetchFilterQualityIncorrectlyAccepted = vi.fn();
const fetchProductionFilterConfig = vi.fn();
const fetchTestFilterConfig = vi.fn();
const saveTestFilterConfig = vi.fn();
const runTestFilterSimulation = vi.fn();
const promoteTestFilterConfig = vi.fn();
const startFilterQualityRun = vi.fn();

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ComposedChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Bar: () => null,
  Scatter: () => null,
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchHealth: (...args: unknown[]) => fetchHealth(...args),
    fetchProviders: (...args: unknown[]) => fetchProviders(...args),
    fetchThroughput: (...args: unknown[]) => fetchThroughput(...args),
    fetchBacklog: (...args: unknown[]) => fetchBacklog(...args),
    fetchDeadLetters: (...args: unknown[]) => fetchDeadLetters(...args),
    fetchFilterQualityStatus: (...args: unknown[]) => fetchFilterQualityStatus(...args),
    fetchFilterQualityIncorrectlyRejected: (...args: unknown[]) => fetchFilterQualityIncorrectlyRejected(...args),
    fetchFilterQualityIncorrectlyAccepted: (...args: unknown[]) => fetchFilterQualityIncorrectlyAccepted(...args),
    fetchProductionFilterConfig: (...args: unknown[]) => fetchProductionFilterConfig(...args),
    fetchTestFilterConfig: (...args: unknown[]) => fetchTestFilterConfig(...args),
    saveTestFilterConfig: (...args: unknown[]) => saveTestFilterConfig(...args),
    runTestFilterSimulation: (...args: unknown[]) => runTestFilterSimulation(...args),
    promoteTestFilterConfig: (...args: unknown[]) => promoteTestFilterConfig(...args),
    startFilterQualityRun: (...args: unknown[]) => startFilterQualityRun(...args),
  };
});

describe("NewsFetcherTab", () => {
  beforeEach(() => {
    fetchHealth.mockResolvedValue({
      readiness: "healthy",
      liveness: "healthy",
      stale_data: false,
      last_successful_refresh_at: "2026-06-16T10:00:00Z",
      dependencies: [],
      active_incident_count: 0,
    });
    fetchProviders.mockResolvedValue({
      available: true,
      message: null,
      providers: [],
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchThroughput.mockResolvedValue({
      available: true,
      message: null,
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-15T10:00:00Z",
      window_end_at: "2026-06-16T10:00:00Z",
      buckets: [],
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchBacklog.mockResolvedValue({
      available: true,
      message: null,
      pending_count: 0,
      retrying_count: 0,
      dead_letter_count: 0,
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchDeadLetters.mockResolvedValue({
      available: true,
      message: null,
      items: [],
      limit: 25,
      offset: 0,
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchFilterQualityStatus.mockResolvedValue({
      available: true,
      message: null,
      running_run: null,
      last_run: null,
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchFilterQualityIncorrectlyRejected.mockResolvedValue({
      run_id: "fqe_1",
      items: [],
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchFilterQualityIncorrectlyAccepted.mockResolvedValue({
      run_id: "fqe_1",
      items: [],
      generated_at: "2026-06-16T10:00:00Z",
    });
    fetchProductionFilterConfig.mockResolvedValue({
      config_name: "Production filter",
      config_role: "production",
      status: "active",
      include_keywords: [],
      exclude_keywords: [],
      watchlist_tickers: [],
      dedupe_algorithm: "rapidfuzz_ratio",
      dedupe_similarity_threshold: 0.9,
      dedupe_lookback_hours: 24,
    });
    fetchTestFilterConfig.mockResolvedValue({
      config_name: "Test filter",
      config_role: "test",
      status: "active",
      include_keywords: [],
      exclude_keywords: [],
      watchlist_tickers: [],
      dedupe_algorithm: "rapidfuzz_ratio",
      dedupe_similarity_threshold: 0.9,
      dedupe_lookback_hours: 24,
    });
    saveTestFilterConfig.mockResolvedValue({});
    runTestFilterSimulation.mockResolvedValue({});
    promoteTestFilterConfig.mockResolvedValue({});
    startFilterQualityRun.mockResolvedValue({ run_id: "fqe_1", status: "running" });
  });

  it("shows degraded panel messaging from available=false responses", async () => {
    fetchThroughput.mockResolvedValue({
      available: false,
      message: "Throughput data unavailable.",
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-15T10:00:00Z",
      window_end_at: "2026-06-16T10:00:00Z",
      buckets: [],
      generated_at: "2026-06-16T10:00:00Z",
    });

    renderWithQueryClient(<NewsFetcherTab />);

    expect((await screen.findAllByText("Throughput data unavailable.")).length).toBeGreaterThan(0);
    expect(screen.getByText("Throughput data unavailable")).toBeInTheDocument();
  });

  it("shows a data source warning banner for handled 503 responses", async () => {
    fetchProductionFilterConfig.mockRejectedValue(
      new ApiError("production filter config unavailable", 503)
    );

    renderWithQueryClient(<NewsFetcherTab />);

    expect(await screen.findByText("production filter config unavailable")).toBeInTheDocument();
  });
});

function renderWithQueryClient(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}
