import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "../api";
import { NewsFetcherTab } from "./NewsFetcherTab";

const fetchHealth = vi.fn();
const fetchProviders = vi.fn();
const fetchThroughput = vi.fn();
const fetchFetchedArticles = vi.fn();
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
const reprocessNewsFetcherRejected = vi.fn();

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ComposedChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  XAxis: ({ domain }: { domain?: [number | string, number | string] }) => (
    <div data-testid="news-fetcher-throughput-x-axis" data-domain={JSON.stringify(domain)} />
  ),
  YAxis: () => null,
  Tooltip: () => null,
  Bar: ({ dataKey, barSize }: { dataKey?: string; barSize?: number }) => (
    <div data-testid={`news-fetcher-throughput-bar-${dataKey}`} data-bar-size={barSize} />
  ),
  Scatter: () => null,
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchHealth: (...args: unknown[]) => fetchHealth(...args),
    fetchProviders: (...args: unknown[]) => fetchProviders(...args),
    fetchThroughput: (...args: unknown[]) => fetchThroughput(...args),
    fetchFetchedArticles: (...args: unknown[]) => fetchFetchedArticles(...args),
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
    reprocessNewsFetcherRejected: (...args: unknown[]) => reprocessNewsFetcherRejected(...args),
  };
});

describe("NewsFetcherTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    fetchFetchedArticles.mockResolvedValue({
      available: true,
      message: null,
      window: "1d",
      items: [],
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
    reprocessNewsFetcherRejected.mockResolvedValue({
      window: "1d",
      scanned_rejected_count: 3,
      newly_accepted_count: 2,
      still_rejected_count: 1,
      already_accepted_count: 0,
      already_published_count: 0,
      queued_publication_obligation_count: 2,
      generated_at: "2026-06-16T10:00:00Z",
    });
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

  it("uses a padded bucket domain and explicit bar sizes for visible throughput bars", async () => {
    fetchThroughput.mockResolvedValue({
      available: true,
      message: null,
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-15T10:00:00Z",
      window_end_at: "2026-06-16T10:00:00Z",
      buckets: [
        {
          window_start: "2026-06-16T09:00:00Z",
          source_key: "rss:cnbc",
          fetch_count: 4,
          publish_success_count: 3,
          publish_error_count: 1,
        },
        {
          window_start: "2026-06-16T09:00:00Z",
          source_key: "rss:yahoo",
          fetch_count: 2,
          publish_success_count: 2,
          publish_error_count: 0,
        },
      ],
      generated_at: "2026-06-16T10:00:00Z",
    });

    renderWithQueryClient(<NewsFetcherTab />);

    const axis = await screen.findByTestId("news-fetcher-throughput-x-axis");
    const bucketStart = new Date("2026-06-16T09:00:00Z").getTime();
    const expectedPaddingMs = 30 * 60 * 1000;
    expect(axis).toHaveAttribute(
      "data-domain",
      JSON.stringify([bucketStart - expectedPaddingMs, bucketStart + expectedPaddingMs])
    );
    expect(screen.getByTestId("news-fetcher-throughput-bar-fetched")).toHaveAttribute("data-bar-size", "12");
    expect(screen.getByTestId("news-fetcher-throughput-bar-published")).toHaveAttribute("data-bar-size", "12");
    expect(screen.getByTestId("news-fetcher-throughput-bar-failed")).toHaveAttribute("data-bar-size", "12");
  });

  it("shows a data source warning banner for handled 503 responses", async () => {
    fetchProductionFilterConfig.mockRejectedValue(
      new ApiError("production filter config unavailable", 503)
    );

    renderWithQueryClient(<NewsFetcherTab />);

    expect(await screen.findByText("production filter config unavailable")).toBeInTheDocument();
  });

  it("collapses generated Finnhub company providers by default", async () => {
    fetchProviders.mockResolvedValue({
      available: true,
      message: null,
      providers: [
        providerStatus("finnhub"),
        providerStatus("finnhub:company:AAPL", { publish_success_count: 2 }),
        providerStatus("finnhub:company:MSFT", { publish_success_count: 3 }),
      ],
      generated_at: "2026-06-16T10:00:00Z",
    });

    renderWithQueryClient(<NewsFetcherTab />);

    const groupRow = await screen.findByText(/finnhub:company:\* \(2\)/);
    expect(groupRow).toBeInTheDocument();
    expect(screen.getByText("finnhub")).toBeInTheDocument();
    expect(screen.queryByText("finnhub:company:AAPL")).not.toBeInTheDocument();
    expect(screen.queryByText("finnhub:company:MSFT")).not.toBeInTheDocument();

    fireEvent.click(groupRow);

    expect(await screen.findByText("finnhub:company:AAPL")).toBeInTheDocument();
    expect(screen.getByText("finnhub:company:MSFT")).toBeInTheDocument();
  });

  it("reprocesses rejected articles for the selected fetched-articles window", async () => {
    renderWithQueryClient(<NewsFetcherTab />);

    const sevenDayButtons = await screen.findAllByRole("button", { name: "7d" });
    fireEvent.click(sevenDayButtons[sevenDayButtons.length - 1]);
    fireEvent.click(screen.getByRole("button", { name: "Reprocess rejected" }));

    await waitFor(() => {
      expect(reprocessNewsFetcherRejected.mock.calls[0][0]).toEqual({ window: "7d" });
    });
    expect(
      await screen.findByText("Reprocessed 3 rejected; 2 newly accepted; 2 queued")
    ).toBeInTheDocument();
  });

  it("shows pending state while rejected articles are reprocessing", async () => {
    let resolveReprocess: (value: unknown) => void = () => {};
    reprocessNewsFetcherRejected.mockReturnValue(
      new Promise((resolve) => {
        resolveReprocess = resolve;
      })
    );
    renderWithQueryClient(<NewsFetcherTab />);

    fireEvent.click(await screen.findByRole("button", { name: "Reprocess rejected" }));

    expect(await screen.findByRole("button", { name: "Reprocessing" })).toBeDisabled();
    resolveReprocess({
      window: "1d",
      scanned_rejected_count: 0,
      newly_accepted_count: 0,
      still_rejected_count: 0,
      already_accepted_count: 0,
      already_published_count: 0,
      queued_publication_obligation_count: 0,
      generated_at: "2026-06-16T10:00:00Z",
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Reprocess rejected" })).toBeInTheDocument();
    });
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

function providerStatus(
  source_key: string,
  overrides: Partial<{
    last_cycle_start_at: string | null;
    last_cycle_end_at: string | null;
    last_cycle_duration_seconds: number | null;
    last_fetch_attempt_at: string | null;
    last_non_zero_fetch_at: string | null;
    fetch_count: number;
    fetch_error_count: number;
    dedupe_drop_count: number;
    persist_success_count: number;
    publish_success_count: number;
    last_error_code: string | null;
    last_error_at: string | null;
  }> = {}
) {
  return {
    source_key,
    last_cycle_start_at: "2026-06-16T09:59:00Z",
    last_cycle_end_at: "2026-06-16T10:00:00Z",
    last_cycle_duration_seconds: 3,
    last_fetch_attempt_at: "2026-06-16T09:59:30Z",
    last_non_zero_fetch_at: null,
    fetch_count: 0,
    fetch_error_count: 0,
    dedupe_drop_count: 0,
    persist_success_count: 0,
    publish_success_count: 0,
    last_error_code: null,
    last_error_at: null,
    ...overrides,
  };
}
