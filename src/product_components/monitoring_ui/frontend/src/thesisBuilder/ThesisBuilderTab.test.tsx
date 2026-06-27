import { fireEvent, render, screen } from "@testing-library/react";

import { ThesisBuilderTab } from "./ThesisBuilderTab";

const fetchThesisBuilderMetrics = vi.fn();
const useQuery = vi.fn();

vi.mock("@tanstack/react-query", () => ({
  useQuery: (...args: unknown[]) => useQuery(...args),
  useMutation: () => ({ mutate: vi.fn(), isPending: false, data: undefined, error: null }),
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchThesisBuilderMetrics: (...args: unknown[]) => fetchThesisBuilderMetrics(...args),
  };
});

describe("ThesisBuilderTab", () => {
  beforeEach(() => {
    useQuery.mockImplementation((options: { queryKey?: unknown[] }) => {
      if (options?.queryKey?.includes("reprocess")) {
        return { data: undefined, error: null };
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
        actionable_cards: [
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
          },
        ],
        generated_at: "2026-06-16T10:00:00Z",
      },
      isError: false,
      error: null,
  };

  it("renders the thesis dead-letter metric and recent rows", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("Dead letters")).toBeInTheDocument();
    expect(screen.getByText("article-1")).toBeInTheDocument();
    expect(screen.getByText("missing_article_payload")).toBeInTheDocument();
  });

  it("renders the actionable thesis cards section and detail on selection", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("Actionable Thesis Cards")).toBeInTheDocument();
    expect(screen.getByText("Select a card to see details.")).toBeInTheDocument();

    fireEvent.click(screen.getByText("AAPL"));

    expect(screen.getByText("Time horizon")).toBeInTheDocument();
    expect(screen.getByText("swing 1d 5d")).toBeInTheDocument();
  });

  it("hides expired cards by default and reveals them when checkbox is checked", () => {
    render(<ThesisBuilderTab />);

    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.queryByText("MSFT")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: /show expired/i }));

    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("Expired 1h 0m ago")).toBeInTheDocument();
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
      return unavailableResult;
    });

    render(<ThesisBuilderTab />);

    expect(screen.getByText("ThesisBuilder telemetry unavailable.")).toBeInTheDocument();
    expect(screen.getByText("ThesisBuilder dead-letter data unavailable")).toBeInTheDocument();
  });
});
