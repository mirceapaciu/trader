import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { WatchlistTab } from "./WatchlistTab";

const fetchWatchlist = vi.fn();
const lookupWatchlist = vi.fn();
const addWatchlistItem = vi.fn();
const updateWatchlistItem = vi.fn();
const discoverWatchlistAliases = vi.fn();
const deactivateWatchlistItem = vi.fn();

vi.mock("../api", () => ({
  fetchWatchlist: (...args: unknown[]) => fetchWatchlist(...args),
  lookupWatchlist: (...args: unknown[]) => lookupWatchlist(...args),
  addWatchlistItem: (...args: unknown[]) => addWatchlistItem(...args),
  updateWatchlistItem: (...args: unknown[]) => updateWatchlistItem(...args),
  discoverWatchlistAliases: (...args: unknown[]) => discoverWatchlistAliases(...args),
  deactivateWatchlistItem: (...args: unknown[]) => deactivateWatchlistItem(...args),
}));

describe("WatchlistTab", () => {
  beforeEach(() => {
    fetchWatchlist.mockReset();
    lookupWatchlist.mockReset();
    addWatchlistItem.mockReset();
    updateWatchlistItem.mockReset();
    discoverWatchlistAliases.mockReset();
    deactivateWatchlistItem.mockReset();

    fetchWatchlist.mockResolvedValue({
      lookup_providers_configured: true,
      lookup_message: null,
      items: [
        {
          ticker: "RHM",
          exchange_code: "XETR",
          display_name: "Rheinmetall",
          aliases: [],
          is_active: true,
          source: "manual",
          has_missing_aliases: true,
        }
      ],
      generated_at: "2026-06-15T00:00:00Z"
    });
    lookupWatchlist.mockResolvedValue({
      query: "nvidia",
      suggestions: [
        {
          ticker: "NVDA",
          exchange_code: "XNAS",
          display_name: "NVIDIA Corporation",
          aliases: ["nvidia", "nvidia corporation"],
          provider: "massive",
        }
      ],
      cached: false,
      generated_at: "2026-06-15T00:00:00Z"
    });
    addWatchlistItem.mockResolvedValue({});
    updateWatchlistItem.mockResolvedValue({});
    discoverWatchlistAliases.mockResolvedValue({
      ticker: "RHM",
      exchange_code: "XETR",
      display_name: "Rheinmetall",
      aliases: ["rheinmetall ag", "rhm"],
      provider: "massive",
      found: true,
      cached: false,
      generated_at: "2026-06-15T00:00:00Z"
    });
    deactivateWatchlistItem.mockResolvedValue(undefined);
  });

  it("renders suggestions, preview, and auto-repairs missing aliases", async () => {
    const rhmItem = {
      ticker: "RHM",
      exchange_code: "XETR",
      display_name: "Rheinmetall",
      aliases: [],
      is_active: true,
      source: "manual",
      has_missing_aliases: true,
    };
    fetchWatchlist
      .mockResolvedValueOnce({
        lookup_providers_configured: true,
        lookup_message: null,
        items: [rhmItem],
        generated_at: "2026-06-15T00:00:00Z"
      })
      .mockResolvedValueOnce({
        lookup_providers_configured: true,
        lookup_message: null,
        items: [{ ...rhmItem, aliases: [], has_missing_aliases: false }],
        generated_at: "2026-06-15T00:00:00Z"
      });

    renderWithQueryClient(<WatchlistTab />);

    expect(await screen.findByText("RHM:XETR")).toBeInTheDocument();
    expect(screen.getByText("No aliases")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/nvidia/i), { target: { value: "nvidia" } });

    expect(await screen.findByText("NVDA:XNAS")).toBeInTheDocument();
    fireEvent.click(screen.getByText("NVDA:XNAS"));

    await waitFor(() => expect(screen.getByTestId("watchlist-preview")).toBeInTheDocument());
    expect(within(screen.getByTestId("watchlist-preview")).getByText("NVIDIA Corporation")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /auto-find aliases/i }));

    await waitFor(() =>
      expect(updateWatchlistItem).toHaveBeenCalledWith("RHM", "XETR", {
        ticker: "RHM",
        exchange_code: "XETR",
        display_name: "Rheinmetall",
        aliases: [],
        source: "manual",
      })
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: /auto-find aliases/i })).not.toBeInTheDocument());
  });

  it("normalizes hyphenated company names before lookup", async () => {
    lookupWatchlist.mockResolvedValueOnce({
      query: "novo nordisk",
      suggestions: [
        {
          ticker: "NVO",
          exchange_code: "XNYS",
          display_name: "Novo Nordisk A/S",
          aliases: ["novo nordisk", "novo nordisk a/s"],
          provider: "massive",
        }
      ],
      cached: false,
      generated_at: "2026-06-15T00:00:00Z"
    });

    renderWithQueryClient(<WatchlistTab />);

    fireEvent.change(screen.getByPlaceholderText(/nvidia/i), { target: { value: "Novo-Nordisk" } });

    expect(await screen.findByText("NVO:XNYS")).toBeInTheDocument();
    await waitFor(() => expect(lookupWatchlist).toHaveBeenCalledWith("Novo Nordisk"));
    expect(screen.getByText(/Searching for:/)).toBeInTheDocument();
  });

  it("looks up single-letter exact ticker searches", async () => {
    lookupWatchlist.mockResolvedValueOnce({
      query: "F",
      suggestions: [
        {
          ticker: "F",
          exchange_code: "XNYS",
          display_name: "Ford Motor Company",
          aliases: ["f", "ford motor company"],
          provider: "massive",
        }
      ],
      cached: false,
      generated_at: "2026-06-15T00:00:00Z"
    });

    renderWithQueryClient(<WatchlistTab />);

    fireEvent.change(screen.getByPlaceholderText(/nvidia/i), { target: { value: "F" } });

    expect(await screen.findByText("F:XNYS")).toBeInTheDocument();
    await waitFor(() => expect(lookupWatchlist).toHaveBeenCalledWith("F"));
    expect(screen.getByText(/Single-letter searches return exact ticker matches./i)).toBeInTheDocument();
  });

  it("calls deactivate API and refreshes watchlist when Deactivate is clicked", async () => {
    const rhmItem = {
      ticker: "RHM",
      exchange_code: "XETR",
      display_name: "Rheinmetall",
      aliases: [],
      is_active: true,
      source: "manual",
      has_missing_aliases: false,
    };
    // First fetch returns RHM; second fetch (after invalidation) returns empty list.
    fetchWatchlist
      .mockResolvedValueOnce({ lookup_providers_configured: true, lookup_message: null, items: [rhmItem], generated_at: "2026-06-15T00:00:00Z" })
      .mockResolvedValueOnce({ lookup_providers_configured: true, lookup_message: null, items: [], generated_at: "2026-06-15T00:00:00Z" });

    renderWithQueryClient(<WatchlistTab />);

    fireEvent.click(await screen.findByRole("button", { name: /deactivate/i }));

    await waitFor(() => expect(deactivateWatchlistItem).toHaveBeenCalledWith("RHM", "XETR"));
    await waitFor(() => expect(screen.queryByText("RHM:XETR")).not.toBeInTheDocument());
  });

  it("edits display name and aliases via the modal and refreshes the watchlist", async () => {
    const rhmItem = {
      ticker: "RHM",
      exchange_code: "XETR",
      display_name: "Rheinmetall",
      aliases: ["rheinmetall"],
      is_active: true,
      source: "manual",
      has_missing_aliases: false,
    };
    fetchWatchlist
      .mockResolvedValueOnce({ lookup_providers_configured: true, lookup_message: null, items: [rhmItem], generated_at: "2026-06-15T00:00:00Z" })
      .mockResolvedValueOnce({
        lookup_providers_configured: true,
        lookup_message: null,
        items: [{ ...rhmItem, display_name: "Rheinmetall AG", aliases: ["rheinmetall", "rheinmetall ag"] }],
        generated_at: "2026-06-15T00:00:00Z",
      });

    renderWithQueryClient(<WatchlistTab />);

    fireEvent.click(await screen.findByRole("button", { name: /^edit$/i }));

    const dialog = await screen.findByRole("dialog");
    // Identity is locked; display name and aliases are editable.
    expect(within(dialog).getByLabelText(/ticker/i)).toBeDisabled();
    expect(within(dialog).getByLabelText(/market code/i)).toBeDisabled();
    const displayNameInput = within(dialog).getByLabelText(/display name/i);
    expect(displayNameInput).not.toBeDisabled();

    fireEvent.change(displayNameInput, { target: { value: "Rheinmetall AG" } });
    fireEvent.change(within(dialog).getByLabelText(/aliases/i), { target: { value: "rheinmetall, rheinmetall ag" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /save changes/i }));

    await waitFor(() =>
      expect(updateWatchlistItem).toHaveBeenCalledWith("RHM", "XETR", {
        ticker: "RHM",
        exchange_code: "XETR",
        display_name: "Rheinmetall AG",
        aliases: ["rheinmetall", "rheinmetall ag"],
        source: "manual",
      })
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Rheinmetall AG")).toBeInTheDocument();
  });

  it("shows a warning and skips lookup when providers are not configured", async () => {
    fetchWatchlist.mockResolvedValueOnce({
      lookup_providers_configured: false,
      lookup_message: "Ticker lookup providers are not configured. Add MASSIVE_API_KEY or ALPHA_VANTAGE_API_KEY to enable suggestions.",
      items: [],
      generated_at: "2026-06-15T00:00:00Z"
    });

    renderWithQueryClient(<WatchlistTab />);

    expect((await screen.findAllByText(/Ticker lookup providers are not configured/i)).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByPlaceholderText(/nvidia/i), { target: { value: "Ford" } });

    await waitFor(() => expect(lookupWatchlist).not.toHaveBeenCalled());
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
