import { fireEvent, render, screen } from "@testing-library/react";

import { BacktesterTab } from "./BacktesterTab";
import { ApiError } from "../api";

const fetchBacktests = vi.fn();
const startBacktest = vi.fn();
const useQuery = vi.fn();
const useMutation = vi.fn();

vi.mock("@tanstack/react-query", () => ({
  useQuery: (...args: unknown[]) => useQuery(...args),
  useMutation: (...args: unknown[]) => useMutation(...args),
  useQueryClient: () => ({ invalidateQueries: vi.fn(), setQueryData: vi.fn() })
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchBacktests: (...args: unknown[]) => fetchBacktests(...args),
    startBacktest: (...args: unknown[]) => startBacktest(...args)
  };
});

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-1234567890",
    status: "completed",
    window_start_at: "2026-06-16T09:00:00Z",
    window_end_at: "2026-06-16T10:00:00Z",
    mode: "replay",
    timing_scenario: "ideal",
    card_population: "all",
    strategies_requested: null,
    initial_capital: 100000,
    net_pnl: 1234.5,
    total_return: 0.0123,
    win_rate: 0.6,
    profit_factor: 1.8,
    max_drawdown: 0.05,
    created_at: "2026-06-16T08:00:00Z",
    started_at: "2026-06-16T08:00:05Z",
    finished_at: "2026-06-16T08:10:00Z",
    error_code: null,
    ...overrides
  };
}

function makeDetail(run: Record<string, unknown>) {
  return {
    available: true,
    message: null,
    run,
    metrics: {
      net_pnl: 1234.5,
      total_return: 0.0123,
      win_rate: 0.6,
      profit_factor: 1.8,
      expectancy: 12.3,
      max_drawdown: 0.05,
      sharpe_ratio: 1.4,
      exposure_fraction: 0.4,
      signal_accuracy: 0.7,
      trades_closed: 10
    },
    per_strategy: [
      {
        strategy: "event_driven",
        trades_opened: 5,
        trades_closed: 5,
        trades_risk_blocked: 0,
        net_pnl: 600,
        win_rate: 0.6,
        avg_win: 200,
        avg_loss: -100,
        profit_factor: 2,
        expectancy: 120
      }
    ],
    card_status_breakdown: [
      {
        bucket: "card_unexpired_at_entry",
        trades_opened: 3,
        trades_closed: 3,
        trades_risk_blocked: 0,
        net_pnl: 400,
        win_rate: 0.66,
        avg_win: 200,
        avg_loss: -50,
        profit_factor: 4,
        expectancy: 130
      }
    ],
    delays: {
      avg_news_fetch_delay_seconds: 30,
      p95_news_fetch_delay_seconds: 60,
      max_news_fetch_delay_seconds: 90,
      avg_thesis_build_delay_seconds: 120,
      p95_thesis_build_delay_seconds: 180,
      max_thesis_build_delay_seconds: 240,
      avg_total_pipeline_delay_seconds: 150,
      p95_total_pipeline_delay_seconds: 240,
      max_total_pipeline_delay_seconds: 330
    },
    gap:
      run.timing_scenario === "both"
        ? { pnl_gap: -100, win_rate_gap: -0.05, trades_flipped_by_delay: 2 }
        : null,
    generated_at: "2026-06-16T08:10:00Z"
  };
}

function backtestsResult(runs: Array<Record<string, unknown>>, overrides: Record<string, unknown> = {}) {
  return {
    data: {
      available: true,
      message: null,
      window: "1d",
      runs,
      active_run: null,
      generated_at: "2026-06-16T08:10:00Z",
      ...overrides
    },
    isError: false,
    error: null,
    isLoading: false
  };
}

const emptyResult = { data: undefined, isError: false, error: null, isLoading: false };

const defaultMutation = { mutate: vi.fn(), isPending: false, data: undefined, error: null };

function installQueryRouter(handlers: {
  backtests?: unknown;
  detail?: unknown;
  equity?: unknown;
  trades?: unknown;
}) {
  useQuery.mockImplementation((options: { queryKey?: unknown[] }) => {
    const key = options?.queryKey ?? [];
    if (key.includes("detail")) {
      return handlers.detail ?? emptyResult;
    }
    if (key.includes("equity")) {
      return handlers.equity ?? emptyResult;
    }
    if (key.includes("trades")) {
      return handlers.trades ?? emptyResult;
    }
    return handlers.backtests ?? emptyResult;
  });
}

describe("BacktesterTab", () => {
  beforeEach(() => {
    useMutation.mockReset();
    useMutation.mockReturnValue(defaultMutation);
    useQuery.mockReset();
  });

  it("renders the run list and switches windows", () => {
    installQueryRouter({ backtests: backtestsResult([makeRun()]), detail: emptyResult });
    render(<BacktesterTab />);

    expect(screen.getByText("Recent Runs")).toBeInTheDocument();
    expect(screen.getByText(/run-1234/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "7d" }));

    // The window query is re-issued with the new preset.
    const windowQueryCalls = useQuery.mock.calls.filter(
      (call) => Array.isArray(call[0]?.queryKey) && call[0].queryKey[0] === "backtests" && call[0].queryKey.length === 2
    );
    expect(windowQueryCalls.some((call) => call[0].queryKey[1] === "7d")).toBe(true);
  });

  it("loads run detail and hides the gap panel for a non-both run", () => {
    const run = makeRun({ timing_scenario: "ideal" });
    installQueryRouter({
      backtests: backtestsResult([run]),
      detail: { ...emptyResult, data: makeDetail(run) }
    });
    render(<BacktesterTab />);

    expect(screen.getByText("Run Summary")).toBeInTheDocument();
    expect(screen.getByText("Sharpe ratio")).toBeInTheDocument();
    expect(screen.getByText("Ideal vs Actual Gap")).toBeInTheDocument();
    expect(screen.getByText(/Gap metrics require a/)).toBeInTheDocument();
    expect(screen.queryByText("Trades flipped by delay")).not.toBeInTheDocument();
  });

  it("shows the gap tiles for a both run", () => {
    const run = makeRun({ timing_scenario: "both" });
    installQueryRouter({
      backtests: backtestsResult([run]),
      detail: { ...emptyResult, data: makeDetail(run) }
    });
    render(<BacktesterTab />);

    expect(screen.getByText("Trades flipped by delay")).toBeInTheDocument();
    expect(screen.getByText("P&L gap")).toBeInTheDocument();
  });

  it("triggers a backtest on submit (happy path)", () => {
    const mutate = vi.fn();
    useMutation.mockReturnValue({ ...defaultMutation, mutate });
    installQueryRouter({ backtests: backtestsResult([]), detail: emptyResult });
    render(<BacktesterTab />);

    fireEvent.change(screen.getByLabelText("Window start (UTC)"), {
      target: { value: "2026-06-16T09:00" }
    });
    fireEvent.change(screen.getByLabelText("Window end (UTC)"), {
      target: { value: "2026-06-16T10:00" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Run backtest" }));

    expect(mutate).toHaveBeenCalledTimes(1);
    const payload = mutate.mock.calls[0][0];
    expect(payload.window_start_at).toBe("2026-06-16T09:00:00Z");
    expect(payload.mode).toBe("replay");
    expect(payload.timing_scenario).toBe("ideal");
  });

  it("shows a busy state when the trigger returns 409", () => {
    useMutation.mockReturnValue({
      ...defaultMutation,
      error: new ApiError("Run already active", 409)
    });
    installQueryRouter({
      backtests: backtestsResult([], { active_run: makeRun({ run_id: "active-999" }) }),
      detail: emptyResult
    });
    render(<BacktesterTab />);

    expect(screen.getByText(/already active/)).toBeInTheDocument();
    expect(screen.getByText(/active-999/)).toBeInTheDocument();
  });

  it("warns when regeneration mode is selected", () => {
    installQueryRouter({ backtests: backtestsResult([]), detail: emptyResult });
    render(<BacktesterTab />);

    expect(screen.queryByText(/Regeneration runs can be long/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "regeneration" } });

    expect(screen.getByText(/Regeneration runs can be long/)).toBeInTheDocument();
  });

  it("renders a degraded empty state when backtests are unavailable", () => {
    installQueryRouter({
      backtests: backtestsResult([], { available: false, message: "Backtester telemetry unavailable." }),
      detail: emptyResult
    });
    render(<BacktesterTab />);

    expect(screen.getAllByText("Backtester telemetry unavailable.").length).toBeGreaterThan(0);
  });
});
