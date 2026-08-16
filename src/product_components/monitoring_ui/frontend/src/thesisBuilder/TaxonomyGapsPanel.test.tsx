import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import { ApiError } from "../api";
import { TaxonomyGapsPanel } from "./TaxonomyGapsPanel";

const fetchGaps = vi.fn();
const fetchValues = vi.fn();
const decideGap = vi.fn();
const fetchDecision = vi.fn();
const fetchAdminSession = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchThesisBuilderTaxonomyGaps: () => fetchGaps(),
    fetchThesisBuilderTaxonomyValues: (params: unknown) => fetchValues(params),
    decideThesisBuilderTaxonomyGap: (payload: unknown) => decideGap(payload),
    fetchThesisBuilderTaxonomyDecision: (commandId: string) => fetchDecision(commandId),
    fetchAdminSession: () => fetchAdminSession(),
  };
});

const gaps = [
  {
    gap_id: 12,
    dimension: "event_stage",
    raw_value: "announcement",
    normalized_proposal: "announcement",
    occurrence_count: 8,
    first_seen_at: "2026-07-20T09:00:00Z",
    last_seen_at: "2026-07-29T09:00:00Z",
    status: "open",
    family_scope: "partnership_joint_venture",
    representative_analysis_ids: [5905],
    representative_headlines: ["Core Scientific announces expanded partnership"],
  },
  {
    gap_id: 13,
    dimension: "coverage_role",
    raw_value: "deal update",
    normalized_proposal: "deal_update",
    occurrence_count: 2,
    first_seen_at: "2026-07-25T09:00:00Z",
    last_seen_at: "2026-07-28T09:00:00Z",
    status: "rejected",
    representative_analysis_ids: [5908],
    representative_headlines: ["Deal update"],
  },
];

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function renderPanel() {
  return render(<TaxonomyGapsPanel />, { wrapper: Wrapper });
}

describe("TaxonomyGapsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchGaps.mockResolvedValue({
      available: true,
      gaps,
      generated_at: "2026-07-29T10:00:00Z",
    });
    fetchValues.mockResolvedValue({
      available: true,
      values: [
        {
          dimension: "event_stage",
          canonical_value: "announced",
          display_name: "Announced",
          family_scope: "partnership_joint_venture",
        },
        {
          dimension: "event_stage",
          canonical_value: "completed",
          display_name: "Completed",
          family_scope: "partnership_joint_venture",
        },
      ],
      generated_at: "2026-07-29T10:00:00Z",
    });
    fetchAdminSession.mockResolvedValue({ authenticated: true, actor: "admin", csrf_token: "test" });
    decideGap.mockResolvedValue({ command_id: "cmd-1", gap_id: 12, action: "map_existing", status: "accepted" });
    fetchDecision.mockResolvedValue({
      command_id: "cmd-1",
      gap_id: 12,
      action: "map_existing",
      status: "completed",
      taxonomy_revision: 4,
      backfill: {
        status: "completed",
        matched_count: 8,
        processed_count: 8,
        changed_count: 7,
        skipped_count: 1,
        failed_count: 0,
      },
    });
  });

  it("filters, sorts, opens the detail drawer, and returns focus when closed", async () => {
    renderPanel();

    const proposal = await screen.findByRole("button", { name: "Announcement" });
    expect(screen.getAllByRole("row")[1]).toHaveTextContent("8");
    fireEvent.change(screen.getByLabelText("Filter taxonomy gaps by status"), { target: { value: "rejected" } });
    expect(screen.queryByRole("button", { name: "Announcement" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deal Update" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter taxonomy gaps by status"), { target: { value: "all" } });

    const restoredProposal = screen.getByRole("button", { name: "Announcement" });
    fireEvent.click(restoredProposal);
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    expect(within(drawer).getByText("announcement")).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: /Analysis 5905/ })).toHaveAttribute(
      "href",
      "#thesis-analysis-5905"
    );
    fireEvent.click(within(drawer).getByRole("button", { name: "Close taxonomy proposal" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(restoredProposal).toHaveFocus();
    expect(proposal).not.toBeNull();
  });

  it("requires rationale and confirmation before mapping, then shows revision and backfill", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Announcement" }));
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    await waitFor(() => expect(fetchValues).toHaveBeenCalledWith({
      dimension: "event_stage",
      familyScope: "partnership_joint_venture",
    }));
    await within(drawer).findByRole("option", { name: "Announced" });

    fireEvent.change(within(drawer).getByLabelText("Canonical value"), { target: { value: "announced" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    expect(within(drawer).getByRole("alert")).toHaveTextContent("Rationale is required");
    fireEvent.change(within(drawer).getByLabelText("Rationale"), { target: { value: "Contract vocabulary uses announced." } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));

    const confirmation = screen.getByRole("alertdialog");
    expect(within(confirmation).getByText(/Map “announcement” as “announced”/)).toBeInTheDocument();
    fireEvent.click(within(confirmation).getByRole("button", { name: "Confirm decision" }));

    await waitFor(() =>
      expect(decideGap).toHaveBeenCalledWith(expect.objectContaining({
        gap_id: 12,
        action: "map_existing",
        target: { canonical_value: "announced" },
        rationale: "Contract vocabulary uses announced.",
        idempotency_key: expect.stringMatching(/^taxonomy-gap-12-map_existing-/),
      }))
    );
    await waitFor(() => expect(within(drawer).getByText("Taxonomy revision")).toBeInTheDocument());
    expect(within(drawer).getByText("4")).toBeInTheDocument();
    expect(within(drawer).getByText("Historical backfill")).toBeInTheDocument();
    expect(within(drawer).getByText("7")).toBeInTheDocument();
  });

  it("preserves the idempotency key and form values when a submission is retried", async () => {
    decideGap
      .mockRejectedValueOnce(new ApiError("temporary outage", 500))
      .mockResolvedValueOnce({ command_id: "cmd-2", gap_id: 12, action: "reject", status: "accepted" });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Announcement" }));
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    fireEvent.click(within(drawer).getByLabelText("Reject"));
    fireEvent.change(within(drawer).getByLabelText("Rejection reason"), { target: { value: "invalid_value" } });
    fireEvent.change(within(drawer).getByLabelText("Rationale"), { target: { value: "This is coverage prose, not an event stage." } });

    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));
    await screen.findByText(/temporary outage/);
    expect(within(drawer).getByLabelText("Rationale")).toHaveValue("This is coverage prose, not an event stage.");

    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(decideGap).toHaveBeenCalledTimes(2));
    expect(decideGap.mock.calls[0][0].idempotency_key).toBe(decideGap.mock.calls[1][0].idempotency_key);
    expect(decideGap.mock.calls[1][0].target).toEqual({ rejection_reason: "invalid_value" });
  });

  it("submits normalized new canonical values without a browser-supplied actor", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Announcement" }));
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    fireEvent.click(within(drawer).getByLabelText("Accept as new"));
    fireEvent.change(within(drawer).getByLabelText("Canonical value"), { target: { value: "Press Announcement" } });
    fireEvent.change(within(drawer).getByLabelText("Display name"), { target: { value: "Press announcement" } });
    fireEvent.change(within(drawer).getByLabelText("Description"), { target: { value: "A formal public announcement." } });
    fireEvent.change(within(drawer).getByLabelText("Identity discriminators"), { target: { value: "channel=press, formal=true" } });
    fireEvent.change(within(drawer).getByLabelText("Rationale"), { target: { value: "Distinct stage supported by repeated examples." } });
    expect(within(drawer).getByText(/press_announcement/)).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));

    await waitFor(() => expect(decideGap).toHaveBeenCalled());
    const payload = decideGap.mock.calls[0][0];
    expect(payload).not.toHaveProperty("actor");
    expect(payload).toEqual(expect.objectContaining({
      action: "accept_new",
      target: expect.objectContaining({
        canonical_value: "press_announcement",
        display_name: "Press announcement",
        family_scope: "partnership_joint_venture",
        identity_discriminators: ["channel=press", "formal=true"],
      }),
    }));
  });

  it("distinguishes conflict and unavailable-authentication errors", async () => {
    decideGap.mockRejectedValueOnce(new ApiError("conflict", 409));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Announcement" }));
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    fireEvent.click(within(drawer).getByLabelText("Reject"));
    fireEvent.change(within(drawer).getByLabelText("Rationale"), { target: { value: "Already represented by another proposal." } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));
    expect(await within(drawer).findByText(/Another operator already decided/)).toBeInTheDocument();
    await waitFor(() => expect(fetchGaps.mock.calls.length).toBeGreaterThan(1));

    decideGap.mockRejectedValueOnce(new ApiError("disabled", 503));
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));
    expect(await within(drawer).findByText(/trusted operator authentication is not configured/)).toBeInTheDocument();
  });

  it("explains an asynchronous optimistic-concurrency conflict and refreshes the gaps", async () => {
    fetchDecision.mockResolvedValueOnce({
      command_id: "cmd-1",
      gap_id: 12,
      action: "reject",
      status: "failed",
      error_code: "taxonomy_gap_conflict",
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Announcement" }));
    const drawer = screen.getByRole("dialog", { name: "Announcement" });
    fireEvent.click(within(drawer).getByLabelText("Reject"));
    fireEvent.change(within(drawer).getByLabelText("Rationale"), {
      target: { value: "This value was decided while the drawer was open." },
    });
    fireEvent.click(within(drawer).getByRole("button", { name: "Review decision" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Confirm decision" }));

    expect(await within(drawer).findByText(/Another operator already decided/)).toBeInTheDocument();
    await waitFor(() => expect(fetchGaps.mock.calls.length).toBeGreaterThan(1));
  });
});
