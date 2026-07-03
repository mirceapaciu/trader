import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { OverviewTab } from "./OverviewTab";

const fetchThroughput = vi.fn();
const fetchThesisBuilderThroughput = vi.fn();

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ComposedChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Bar: () => null,
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchThroughput: (...args: unknown[]) => fetchThroughput(...args),
    fetchThesisBuilderThroughput: (...args: unknown[]) => fetchThesisBuilderThroughput(...args),
  };
});

describe("OverviewTab", () => {
  beforeEach(() => {
    fetchThroughput.mockResolvedValue({
      available: true,
      message: null,
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-16T09:00:00Z",
      window_end_at: "2026-06-16T11:00:00Z",
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
      generated_at: "2026-06-16T11:01:00Z",
    });
    fetchThesisBuilderThroughput.mockResolvedValue({
      available: true,
      message: null,
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-16T09:00:00Z",
      window_end_at: "2026-06-16T11:00:00Z",
      buckets: [
        {
          window_start: "2026-06-16T09:00:00Z",
          consumed_messages_count: 5,
          skipped_messages_count: 1,
          failed_messages_count: 0,
          processed_articles_count: 4,
          news_catalyst_articles_count: 3,
          market_moving_articles_count: 2,
        },
      ],
      generated_at: "2026-06-16T11:02:00Z",
    });
  });

  it("renders cross-pipeline throughput totals", async () => {
    renderWithQueryClient(<OverviewTab />);

    expect(await screen.findByText("Pipeline Throughput")).toBeInTheDocument();
    expect(screen.getAllByText("Published to queue").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Processed by ThesisBuilder").length).toBeGreaterThan(0);
    expect(screen.getByText("Unprocessed delta")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("5").length).toBeGreaterThanOrEqual(2);
      expect(screen.getByText("0")).toBeInTheDocument();
      expect(screen.getByText("1")).toBeInTheDocument();
    });
  });

  it("refetches both series when the window changes", async () => {
    renderWithQueryClient(<OverviewTab />);

    fireEvent.click(await screen.findByRole("button", { name: "7d" }));

    await waitFor(() => {
      expect(fetchThroughput).toHaveBeenCalledWith({ window: "7d" });
      expect(fetchThesisBuilderThroughput).toHaveBeenCalledWith("7d");
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
