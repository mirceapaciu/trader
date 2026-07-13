import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import { ThesisBuilderTab } from "./ThesisBuilderTab";

const fetchThesisBuilderMetrics = vi.fn();
const fetchThesisBuilderThroughput = vi.fn();
const fetchThesisCards = vi.fn();
const useQuery = vi.fn();

vi.mock("@tanstack/react-query", () => ({
  useQuery: (...args: unknown[]) => useQuery(...args),
  useMutation: () => ({ mutate: vi.fn(), isPending: false, data: undefined, error: null }),
}));

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ComposedChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  XAxis: ({ domain }: { domain?: [number | string, number | string] }) => (
    <div data-testid="thesis-builder-throughput-x-axis" data-domain={JSON.stringify(domain)} />
  ),
  YAxis: () => null,
  Tooltip: () => null,
  Bar: ({ dataKey, barSize }: { dataKey?: string; barSize?: number }) => (
    <div data-testid={`thesis-builder-throughput-bar-${dataKey}`} data-bar-size={barSize} />
  ),
  Scatter: () => null,
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchThesisBuilderMetrics: (...args: unknown[]) => fetchThesisBuilderMetrics(...args),
    fetchThesisBuilderThroughput: (...args: unknown[]) => fetchThesisBuilderThroughput(...args),
    fetchThesisCards: (...args: unknown[]) => fetchThesisCards(...args),
  };
});

describe("ThesisBuilderTab", () => {
  beforeEach(() => {
    useQuery.mockImplementation((options: { queryKey?: unknown[] }) => {
      if (options?.queryKey?.includes("reprocess")) {
        return { data: undefined, error: null };
      }
      if (options?.queryKey?.includes("cards")) {
        return cardsQueryResult;
      }
      if (options?.queryKey?.includes("throughput")) {
        return throughputQueryResult;
      }
      if (options?.queryKey?.includes("analyses")) {
        return analysesQueryResult;
      }
      return metricsQueryResult;
    });
  });

  const metricsQueryResult = {
      data: {
        available: true,
        message: null,
        window: "1d",
        window_start_at: "2026-06-16T09:00:00Z",
        window_end_at: "2026-06-16T10:00:00Z",
        articles_processed_count: 5,
        market_moving_articles_count: 3,
        articles_included_in_cards_count: 4,
        stale_articles_count: 1,
        created_thesis_cards_count: 2,
        pending_thesis_cards_count: 1,
        oldest_pending_age_seconds: 600,
        average_pending_age_seconds: 300,
        minimum_pending_expires_in_seconds: 1200,
        average_pending_expires_in_seconds: 1500,
        missed_stale_thesis_cards_count: 1,
        stale_evidence_exceeded_avg_seconds: 300,
        stale_evidence_exceeded_p95_seconds: 540,
        stale_evidence_exceeded_max_seconds: 600,
        dead_letter_count: 1,
        recent_dead_letters: [
          {
            source_message_id: "1781810262488-0",
            article_id: "article-1",
            error_code: "missing_article_payload",
            failed_at: "2026-06-16T10:00:00Z",
          },
        ],
        pending_windows: [],
        actionable_cards: [],
        generated_at: "2026-06-16T10:00:00Z",
      },
      isError: false,
      error: null,
  };

  const cardsQueryResult = {
    data: {
      available: true,
      message: null,
      window: "1d",
      window_start_at: "2026-06-16T09:00:00Z",
      window_end_at: "2026-06-16T10:00:00Z",
      cards: [
        {
          card_id: "card-1",
          ticker: "AAPL",
          exchange_code: "NASDAQ",
          strategy: "event_driven",
          direction: "buy",
          time_horizon: "swing_1d_5d",
          confidence: 0.82,
          created_at: "2026-06-16T09:30:00Z",
          expires_at: "2026-06-17T09:30:00Z",
          expires_in_seconds: 86400,
          evidence_count: 3,
          signal_published: false,
          validation_status: "valid",
          rejection_reason_code: null,
        },
        {
          card_id: "card-2",
          ticker: "MSFT",
          exchange_code: "NASDAQ",
          strategy: "sentiment_momentum",
          direction: "sell",
          time_horizon: "swing_1d_5d",
          confidence: 0.75,
          created_at: "2026-06-15T08:00:00Z",
          expires_at: "2026-06-15T20:00:00Z",
          expires_in_seconds: -3600,
          evidence_count: 2,
          signal_published: true,
          validation_status: "valid",
          rejection_reason_code: null,
        },
        {
          card_id: "card-3",
          ticker: "TSLA",
          exchange_code: "NASDAQ",
          strategy: "contrarian_reversal",
          direction: "sell",
          time_horizon: "swing_1d_5d",
          confidence: 0.41,
          created_at: "2026-06-16T09:45:00Z",
          expires_at: "2026-06-16T11:45:00Z",
          expires_in_seconds: 7200,
          evidence_count: 1,
          signal_published: false,
          validation_status: "rejected",
          rejection_reason_code: "stale_evidence",
        },
      ],
      generated_at: "2026-06-16T10:00:00Z",
    },
    isError: false,
    isLoading: false,
    error: null,
  };

  const throughputQueryResult = {
    data: {
      available: true,
      message: null,
      window: "1d",
      granularity: "hour",
      window_start_at: "2026-06-16T09:00:00Z",
      window_end_at: "2026-06-16T10:00:00Z",
      buckets: [
        {
          window_start: "2026-06-16T09:00:00Z",
          processed_articles_count: 5,
          news_catalyst_articles_count: 2,
          market_moving_articles_count: 3,
        },
      ],
      generated_at: "2026-06-16T10:00:00Z",
    },
    isError: false,
    isLoading: false,
    error: null,
  };

  const analysesQueryResult = {
    data: {
      available: true,
      message: null,
      items: [],
      has_more: false,
      generated_at: "2026-06-16T10:00:00Z",
    },
    isError: false,
    isLoading: false,
    error: null,
  };

  it("renders the thesis dead-letter metric and recent rows", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("Dead letters")).toBeInTheDocument();
    expect(screen.getByText("article-1")).toBeInTheDocument();
    expect(screen.getByText("missing_article_payload")).toBeInTheDocument();
  });

  it("renders the throughput chart labels", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("Throughput")).toBeInTheDocument();
    expect(screen.getByText("Processed")).toBeInTheDocument();
    expect(screen.getAllByText("News catalyst").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Market moving").length).toBeGreaterThan(0);
    expect(
      useQuery.mock.calls.some(
        ([options]: Array<{ queryKey?: unknown[] } | undefined>) =>
          options?.queryKey?.[0] === "thesis-builder" &&
          options?.queryKey?.[1] === "throughput" &&
          options?.queryKey?.[2] === "1d"
      )
    ).toBe(true);
  });

  it("uses a padded bucket domain and explicit bar sizes for visible throughput bars", () => {
    render(<ThesisBuilderTab />);

    const axis = screen.getByTestId("thesis-builder-throughput-x-axis");
    const bucketStart = new Date("2026-06-16T09:00:00Z").getTime();
    const expectedPaddingMs = 30 * 60 * 1000;
    expect(axis).toHaveAttribute(
      "data-domain",
      JSON.stringify([bucketStart - expectedPaddingMs, bucketStart + expectedPaddingMs])
    );
    expect(screen.getByTestId("thesis-builder-throughput-bar-processed")).toHaveAttribute("data-bar-size", "12");
    expect(screen.getByTestId("thesis-builder-throughput-bar-newsCatalyst")).toHaveAttribute("data-bar-size", "12");
    expect(screen.getByTestId("thesis-builder-throughput-bar-marketMoving")).toHaveAttribute("data-bar-size", "12");
  });

  it("switches the throughput query window when a preset is selected", () => {
    render(<ThesisBuilderTab />);

    fireEvent.click(screen.getByRole("button", { name: "7d" }));

    expect(
      useQuery.mock.calls.some(
        ([options]: Array<{ queryKey?: unknown[] } | undefined>) =>
          options?.queryKey?.[0] === "thesis-builder" &&
          options?.queryKey?.[1] === "throughput" &&
          options?.queryKey?.[2] === "7d"
      )
    ).toBe(true);
  });

  it("renders the thesis cards section and detail on selection", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("Thesis Cards")).toBeInTheDocument();
    expect(screen.getByText("Select a card to see details.")).toBeInTheDocument();

    fireEvent.click(screen.getByText("AAPL"));

    expect(screen.getByText("Time horizon")).toBeInTheDocument();
    expect(screen.getByText("swing 1d 5d")).toBeInTheDocument();
  });

  it("defaults to valid buy/sell live cards and hides rejected/expired", () => {
    render(<ThesisBuilderTab />);

    // AAPL: valid buy, live -> visible
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    // MSFT: valid sell, expired -> hidden by default
    expect(screen.queryByText("MSFT")).not.toBeInTheDocument();
    // TSLA: rejected -> hidden by default
    expect(screen.queryByText("TSLA")).not.toBeInTheDocument();
  });

  it("reveals expired cards when the checkbox is checked", () => {
    render(<ThesisBuilderTab />);

    expect(screen.queryByText("MSFT")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: /show expired/i }));

    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("Expired 1h 0m ago")).toBeInTheDocument();
  });

  it("reveals rejected cards when the status filter is switched to rejected", () => {
    render(<ThesisBuilderTab />);

    expect(screen.queryByText("TSLA")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter by validation status"), {
      target: { value: "rejected" },
    });

    expect(screen.getByText("TSLA")).toBeInTheDocument();
    // AAPL (valid) is now filtered out
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("TSLA"));
    expect(screen.getByText("Rejection reason")).toBeInTheDocument();
    expect(screen.getByText("stale evidence")).toBeInTheDocument();
  });

  it("filters by ticker search", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("AAPL")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter by ticker"), {
      target: { value: "msft" },
    });

    // AAPL no longer matches the search; MSFT is expired so still hidden -> empty state
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
    expect(screen.getByText("No cards match the current filters.")).toBeInTheDocument();
  });

  it("shows a thesis-specific unavailable empty state for dead letters", () => {
    const unavailableResult = {
      data: {
        available: false,
        message: "ThesisBuilder telemetry unavailable.",
        window: "1d",
        window_start_at: "2026-06-16T09:00:00Z",
        window_end_at: "2026-06-16T10:00:00Z",
        articles_processed_count: 0,
        market_moving_articles_count: 0,
        articles_included_in_cards_count: 0,
        stale_articles_count: 0,
        created_thesis_cards_count: 0,
        pending_thesis_cards_count: 0,
        missed_stale_thesis_cards_count: 0,
        dead_letter_count: 0,
        recent_dead_letters: [],
        pending_windows: [],
        actionable_cards: [],
        generated_at: "2026-06-16T10:00:00Z",
      },
      isError: false,
      error: null,
    };
    useQuery.mockImplementation((options: { queryKey?: unknown[] }) => {
      if (options?.queryKey?.includes("reprocess")) {
        return { data: undefined, error: null };
      }
      if (options?.queryKey?.includes("cards")) {
        return cardsQueryResult;
      }
      if (options?.queryKey?.includes("throughput")) {
        return {
          data: {
            available: false,
            message: "ThesisBuilder throughput unavailable.",
            window: "1d",
            granularity: "hour",
            window_start_at: "2026-06-16T09:00:00Z",
            window_end_at: "2026-06-16T10:00:00Z",
            buckets: [],
            generated_at: "2026-06-16T10:00:00Z",
          },
          isError: false,
          error: null,
        };
      }
      if (options?.queryKey?.includes("analyses")) {
        return analysesQueryResult;
      }
      return unavailableResult;
    });

    render(<ThesisBuilderTab />);

    expect(screen.getByText("ThesisBuilder telemetry unavailable.")).toBeInTheDocument();
    expect(screen.getByText("ThesisBuilder throughput unavailable.")).toBeInTheDocument();
    expect(screen.getByText("ThesisBuilder dead-letter data unavailable")).toBeInTheDocument();
  });
});
