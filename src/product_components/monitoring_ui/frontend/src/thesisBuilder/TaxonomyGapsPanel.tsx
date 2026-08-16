import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  ApiError,
  decideThesisBuilderTaxonomyGap,
  fetchAdminSession,
  fetchThesisBuilderTaxonomyDecision,
  fetchThesisBuilderTaxonomyGaps,
  fetchThesisBuilderTaxonomyValues,
  loginAdmin,
  logoutAdmin,
  type ThesisBuilderTaxonomyDecisionRequest,
  type ThesisBuilderTaxonomyDecisionResponse,
  type ThesisBuilderTaxonomyGap,
} from "../api";

type DecisionAction = ThesisBuilderTaxonomyDecisionRequest["action"];
type SortMode = "occurrence_count" | "last_seen";

const TERMINAL_COMMAND_STATES = new Set(["completed", "failed"]);
const TERMINAL_BACKFILL_STATES = new Set(["completed", "failed"]);
const REJECTION_REASONS = [
  ["", "No controlled reason"],
  ["not_an_event", "Not an event"],
  ["duplicate", "Duplicate proposal"],
  ["too_specific", "Too specific"],
  ["invalid_value", "Invalid value"],
] as const;

function labelToken(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizedValue(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function makeIdempotencyKey(gapId: number, action: DecisionAction): string {
  const suffix =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `taxonomy-gap-${gapId}-${action}-${suffix}`;
}

function isFullyTerminal(status?: ThesisBuilderTaxonomyDecisionResponse | null): boolean {
  if (!status || !TERMINAL_COMMAND_STATES.has(status.status)) return false;
  return !status.backfill || TERMINAL_BACKFILL_STATES.has(status.backfill.status);
}

function parseDiscriminators(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function operatorError(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : "The command could not be submitted.";
  if (error.status === 401 || error.status === 403) {
    return "You are not authorized to decide taxonomy proposals. Sign in with an operator account and retry.";
  }
  if (error.status === 409) {
    return "Another operator already decided this proposal. The gap list has been refreshed.";
  }
  if (error.status === 503) {
    return "Taxonomy decisions are unavailable because trusted operator authentication is not configured.";
  }
  if (error.status === 422 || error.status === 400) {
    return `The decision was not valid: ${error.message}`;
  }
  return `The taxonomy command could not be submitted: ${error.message}`;
}

export function TaxonomyGapsPanel() {
  const [dimensionFilter, setDimensionFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortMode, setSortMode] = useState<SortMode>("occurrence_count");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [commandByGap, setCommandByGap] = useState<Record<number, string>>({});
  const query = useQuery({
    queryKey: ["thesis-builder", "taxonomy-gaps"],
    queryFn: fetchThesisBuilderTaxonomyGaps,
    refetchInterval: 60000,
  });
  const gaps = query.data?.gaps ?? [];
  const dimensions = useMemo(
    () => [...new Set(gaps.map((gap) => gap.dimension))].sort(),
    [gaps]
  );
  const statuses = useMemo(
    () => [...new Set(gaps.map((gap) => gap.status))].sort(),
    [gaps]
  );
  const visibleGaps = useMemo(
    () =>
      gaps
        .filter((gap) => dimensionFilter === "all" || gap.dimension === dimensionFilter)
        .filter((gap) => statusFilter === "all" || gap.status === statusFilter)
        .sort((left, right) =>
          sortMode === "occurrence_count"
            ? right.occurrence_count - left.occurrence_count ||
              Date.parse(right.last_seen_at) - Date.parse(left.last_seen_at)
            : Date.parse(right.last_seen_at) - Date.parse(left.last_seen_at)
        ),
    [dimensionFilter, gaps, sortMode, statusFilter]
  );
  const selectedGap = gaps.find((gap) => gap.gap_id === selectedId) ?? null;

  return (
    <section className="panel panel-large taxonomy-panel" style={{ marginBottom: 20 }}>
      <div className="panel-heading">
        <div>
          <h2>Taxonomy gaps</h2>
          <span>Review unmapped controlled event-identity values</span>
        </div>
      </div>
      <div className="taxonomy-filters">
        <label>
          Dimension
          <select
            aria-label="Filter taxonomy gaps by dimension"
            value={dimensionFilter}
            onChange={(event) => setDimensionFilter(event.target.value)}
          >
            <option value="all">All dimensions</option>
            {dimensions.map((dimension) => (
              <option key={dimension} value={dimension}>{labelToken(dimension)}</option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select
            aria-label="Filter taxonomy gaps by status"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="all">All statuses</option>
            {statuses.map((status) => (
              <option key={status} value={status}>{labelToken(status)}</option>
            ))}
          </select>
        </label>
        <label>
          Sort
          <select
            aria-label="Sort taxonomy gaps"
            value={sortMode}
            onChange={(event) => setSortMode(event.target.value as SortMode)}
          >
            <option value="occurrence_count">Most occurrences</option>
            <option value="last_seen">Most recently seen</option>
          </select>
        </label>
      </div>
      {query.isError || (query.data && !query.data.available) ? (
        <div className="inline-error" role="alert">
          {query.error instanceof Error
            ? query.error.message
            : query.data?.message ?? "Taxonomy gaps unavailable."}
        </div>
      ) : visibleGaps.length === 0 ? (
        <div className="empty">
          {query.isLoading ? "Loading taxonomy gaps…" : gaps.length ? "No taxonomy gaps match the filters." : "No taxonomy gaps recorded."}
        </div>
      ) : (
        <div className="pending-list-wrap">
          <table>
            <thead>
              <tr>
                <th>Proposal</th>
                <th>Dimension</th>
                <th>Seen</th>
                <th>Status</th>
                <th>Representative headline</th>
              </tr>
            </thead>
            <tbody>
              {visibleGaps.map((gap) => (
                <TaxonomyGapRow
                  key={gap.gap_id}
                  gap={gap}
                  pending={Boolean(commandByGap[gap.gap_id])}
                  selected={selectedId === gap.gap_id}
                  onSelect={(opener) => {
                    opener.dataset.taxonomyOpener = String(gap.gap_id);
                    setSelectedId(gap.gap_id);
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selectedGap && (
        <TaxonomyGapDrawer
          gap={selectedGap}
          initialCommandId={commandByGap[selectedGap.gap_id] ?? selectedGap.command_id ?? null}
          onCommandAccepted={(commandId) =>
            setCommandByGap((current) => ({ ...current, [selectedGap.gap_id]: commandId }))
          }
          onTerminal={() => {
            setCommandByGap((current) => {
              const next = { ...current };
              delete next[selectedGap.gap_id];
              return next;
            });
            void query.refetch();
          }}
          onConflict={() => void query.refetch()}
          onClose={() => {
            setSelectedId(null);
            window.setTimeout(() => {
              document
                .querySelector<HTMLElement>(`[data-taxonomy-opener="${selectedGap.gap_id}"]`)
                ?.focus();
            });
          }}
        />
      )}
    </section>
  );
}

function TaxonomyGapRow({
  gap,
  pending,
  selected,
  onSelect,
}: {
  gap: ThesisBuilderTaxonomyGap;
  pending: boolean;
  selected: boolean;
  onSelect: (opener: HTMLButtonElement) => void;
}) {
  const status = pending ? "command pending" : gap.status;
  return (
    <tr className={selected ? "selected" : undefined}>
      <td>
        <button
          type="button"
          className="taxonomy-proposal-button"
          onClick={(event) => onSelect(event.currentTarget)}
          aria-haspopup="dialog"
        >
          {labelToken(gap.normalized_proposal)}
        </button>
        <span className="table-subtext">raw: {gap.raw_value}</span>
      </td>
      <td>{labelToken(gap.dimension)}</td>
      <td>{gap.occurrence_count}</td>
      <td><span className={status === "open" ? "chip warning" : "chip"}>{labelToken(status)}</span></td>
      <td className="analysis-headline-cell">
        {gap.representative_headlines[0] ?? "—"}
        <span className="table-subtext">last seen {new Date(gap.last_seen_at).toLocaleString()}</span>
      </td>
    </tr>
  );
}

function TaxonomyGapDrawer({
  gap,
  initialCommandId,
  onCommandAccepted,
  onTerminal,
  onConflict,
  onClose,
}: {
  gap: ThesisBuilderTaxonomyGap;
  initialCommandId: string | null;
  onCommandAccepted: (commandId: string) => void;
  onTerminal: () => void;
  onConflict: () => void;
  onClose: () => void;
}) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const notifiedTerminal = useRef("");
  const [action, setAction] = useState<DecisionAction>("map_existing");
  const [canonicalSearch, setCanonicalSearch] = useState("");
  const [canonicalValue, setCanonicalValue] = useState("");
  const [displayName, setDisplayName] = useState(labelToken(gap.normalized_proposal));
  const [description, setDescription] = useState("");
  const [familyScope, setFamilyScope] = useState(gap.family_scope ?? "");
  const [discriminators, setDiscriminators] = useState("");
  const [rejectionReason, setRejectionReason] = useState("");
  const [rationale, setRationale] = useState("");
  const [validationError, setValidationError] = useState("");
  const [submissionError, setSubmissionError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [commandId, setCommandId] = useState<string | null>(initialCommandId);
  const [lastCommand, setLastCommand] = useState<ThesisBuilderTaxonomyDecisionResponse | null>(null);
  const [loginUsername, setLoginUsername] = useState("admin");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const session = useQuery({ queryKey: ["admin-session"], queryFn: fetchAdminSession, staleTime: 30000 });
  const login = useMutation({
    mutationFn: () => loginAdmin(loginUsername, loginPassword),
    onSuccess: () => { setLoginPassword(""); setLoginError(""); void session.refetch(); },
    onError: () => setLoginError("Sign-in failed. Check your credentials and try again."),
  });
  const logout = useMutation({ mutationFn: logoutAdmin, onSuccess: () => void session.refetch() });
  const values = useQuery({
    queryKey: ["thesis-builder", "taxonomy-values", gap.dimension, gap.family_scope ?? ""],
    queryFn: () =>
      fetchThesisBuilderTaxonomyValues({
        dimension: gap.dimension,
        familyScope: gap.family_scope,
      }),
    enabled: action === "map_existing",
    staleTime: 60000,
  });
  const command = useQuery({
    queryKey: ["thesis-builder", "taxonomy-decision", commandId],
    queryFn: () => fetchThesisBuilderTaxonomyDecision(commandId as string),
    enabled: commandId !== null,
    refetchInterval: (current) =>
      isFullyTerminal(current.state.data) ? false : 1500,
  });
  const mutation = useMutation({
    mutationFn: decideThesisBuilderTaxonomyGap,
    onSuccess: (result) => {
      setSubmissionError("");
      setLastCommand(result);
      setCommandId(result.command_id);
      onCommandAccepted(result.command_id);
      if (isFullyTerminal(result)) {
        notifiedTerminal.current = `${result.command_id}:${result.status}`;
        onTerminal();
      }
    },
    onError: (error) => {
      setSubmissionError(operatorError(error));
      if (error instanceof ApiError && error.status === 409) onConflict();
    },
  });

  const status = command.data ?? lastCommand;
  const pending = mutation.isPending || Boolean(commandId && !TERMINAL_COMMAND_STATES.has(status?.status ?? ""));
  const filteredValues = (values.data?.values ?? []).filter((value) => {
    const search = canonicalSearch.trim().toLowerCase();
    return (
      !search ||
      value.canonical_value.toLowerCase().includes(search) ||
      (value.display_name ?? "").toLowerCase().includes(search)
    );
  });
  const acceptPreview = normalizedValue(canonicalValue || gap.normalized_proposal);

  useEffect(() => {
    closeButton.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (confirming) setConfirming(false);
        else if (!pending) onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [confirming, onClose, pending]);

  useEffect(() => {
    if (!command.data || !isFullyTerminal(command.data)) return;
    const notificationKey = `${command.data?.command_id}:${command.data?.status}:${command.data?.backfill?.status ?? ""}`;
    if (notifiedTerminal.current === notificationKey) return;
    notifiedTerminal.current = notificationKey;
    setLastCommand(command.data);
    onTerminal();
  }, [command.data, onTerminal]);

  const requestConfirmation = () => {
    setValidationError("");
    setSubmissionError("");
    if (!rationale.trim()) {
      setValidationError("Rationale is required.");
      return;
    }
    if (action === "map_existing" && !canonicalValue) {
      setValidationError("Select a canonical value to map this proposal.");
      return;
    }
    if (action === "accept_new" && !acceptPreview) {
      setValidationError("Enter a canonical value.");
      return;
    }
    if (!idempotencyKey) setIdempotencyKey(makeIdempotencyKey(gap.gap_id, action));
    setConfirming(true);
  };

  const submit = () => {
    const target =
      action === "map_existing"
        ? { canonical_value: canonicalValue }
        : action === "accept_new"
          ? {
              canonical_value: acceptPreview,
              display_name: displayName.trim(),
              description: description.trim(),
              family_scope: familyScope.trim() || null,
              identity_discriminators: parseDiscriminators(discriminators),
            }
          : { rejection_reason: rejectionReason || undefined };
    mutation.mutate({
      gap_id: gap.gap_id,
      expected_gap_status: gap.status,
      action,
      target,
      rationale: rationale.trim(),
      idempotency_key: idempotencyKey || makeIdempotencyKey(gap.gap_id, action),
    });
    setConfirming(false);
  };

  return (
    <div className="taxonomy-drawer-backdrop" onMouseDown={(event) => {
      if (event.currentTarget === event.target && !pending) onClose();
    }}>
      <aside
        className="taxonomy-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="taxonomy-gap-title"
      >
        <div className="modal-header">
          <div>
            <h3 id="taxonomy-gap-title">{labelToken(gap.normalized_proposal)}</h3>
            <span className="table-subtext">{labelToken(gap.dimension)} proposal</span>
          </div>
          <button
            ref={closeButton}
            type="button"
            className="modal-close"
            onClick={onClose}
            disabled={pending}
            aria-label="Close taxonomy proposal"
          >
            ×
          </button>
        </div>
        <div className="taxonomy-drawer-body">
          <dl className="taxonomy-gap-facts">
            <div><dt>Raw value</dt><dd>{gap.raw_value}</dd></div>
            <div><dt>Family scope</dt><dd>{gap.family_scope || "All families"}</dd></div>
            <div><dt>Occurrences</dt><dd>{gap.occurrence_count}</dd></div>
            <div><dt>First seen</dt><dd>{new Date(gap.first_seen_at).toLocaleString()}</dd></div>
            <div><dt>Last seen</dt><dd>{new Date(gap.last_seen_at).toLocaleString()}</dd></div>
            <div><dt>Status</dt><dd>{labelToken(gap.status)}</dd></div>
          </dl>

          {(gap.representative_headlines.length > 0 || gap.representative_analysis_ids.length > 0) && (
            <section aria-labelledby="taxonomy-examples-heading">
              <h4 id="taxonomy-examples-heading">Representative analyses</h4>
              <ul className="taxonomy-examples">
                {gap.representative_analysis_ids.map((analysisId, index) => (
                  <li key={analysisId}>
                    <a href={`#thesis-analysis-${analysisId}`} onClick={onClose}>
                      Analysis {analysisId}: {gap.representative_headlines[index] ?? "Open analysis detail"}
                    </a>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {status && <TaxonomyCommandStatus status={status} />}
          {session.data?.authenticated && (
            <div className="taxonomy-admin-bar"><span>Signed in as <strong>admin</strong></span><button type="button" className="secondary-button" onClick={() => logout.mutate()}>Log out</button></div>
          )}
          {session.data && !session.data.authenticated && (
            <form className="taxonomy-login" onSubmit={(event) => { event.preventDefault(); login.mutate(); }}>
              <h4>Admin sign-in required</h4>
              <label>Username<input autoComplete="username" value={loginUsername} onChange={(event) => setLoginUsername(event.target.value)} /></label>
              <label>Password<input type="password" autoComplete="current-password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} /></label>
              {loginError && <div className="inline-error" role="alert">{loginError}</div>}
              <button type="submit" className="primary-button" disabled={login.isPending}>{login.isPending ? "Signing in…" : "Sign in"}</button>
            </form>
          )}
          {(!session.data || session.data.authenticated) && !pending && status?.status !== "completed" && (
            <form noValidate onSubmit={(event) => { event.preventDefault(); requestConfirmation(); }}>
              <fieldset className="taxonomy-action-picker">
                <legend>Decision</legend>
                {([
                  ["map_existing", "Map to existing"],
                  ["accept_new", "Accept as new"],
                  ["reject", "Reject"],
                ] as Array<[DecisionAction, string]>).map(([value, label]) => (
                  <label key={value}>
                    <input
                      type="radio"
                      name={`taxonomy-action-${gap.gap_id}`}
                      value={value}
                      checked={action === value}
                      onChange={() => {
                        setAction(value);
                        setValidationError("");
                        setIdempotencyKey("");
                      }}
                    />
                    {label}
                  </label>
                ))}
              </fieldset>

              {action === "map_existing" && (
                <div className="taxonomy-form-fields">
                  <label>
                    Search canonical values
                    <input value={canonicalSearch} onChange={(event) => setCanonicalSearch(event.target.value)} />
                  </label>
                  <label>
                    Canonical value
                    <select
                      value={canonicalValue}
                      onChange={(event) => setCanonicalValue(event.target.value)}
                      aria-describedby={values.isError ? "taxonomy-values-error" : undefined}
                    >
                      <option value="">Select a value</option>
                      {filteredValues.map((value) => (
                        <option key={value.canonical_value} value={value.canonical_value}>
                          {value.display_name || labelToken(value.canonical_value)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {values.isLoading && <span className="table-subtext">Loading scoped canonical values…</span>}
                  {values.isError && (
                    <div id="taxonomy-values-error" className="inline-error" role="alert">
                      Canonical values could not be loaded. Retry before mapping.
                    </div>
                  )}
                </div>
              )}

              {action === "accept_new" && (
                <div className="taxonomy-form-fields">
                  <label>
                    Canonical value
                    <input
                      value={canonicalValue || gap.normalized_proposal}
                      onChange={(event) => setCanonicalValue(event.target.value)}
                    />
                  </label>
                  <div className="taxonomy-preview">
                    Normalized preview: <code>{acceptPreview || "—"}</code>
                  </div>
                  <label>
                    Display name
                    <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
                  </label>
                  <label>
                    Description
                    <textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={3} />
                  </label>
                  <label>
                    Family scope
                    <input
                      value={familyScope}
                      onChange={(event) => setFamilyScope(event.target.value)}
                      placeholder="Optional canonical event family"
                    />
                  </label>
                  <label>
                    Identity discriminators
                    <input
                      value={discriminators}
                      onChange={(event) => setDiscriminators(event.target.value)}
                      placeholder="key=value, key=value"
                    />
                  </label>
                </div>
              )}

              {action === "reject" && (
                <label className="taxonomy-form-field">
                  Rejection reason
                  <select value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)}>
                    {REJECTION_REASONS.map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
              )}

              <label className="taxonomy-form-field">
                Rationale
                <textarea
                  value={rationale}
                  onChange={(event) => setRationale(event.target.value)}
                  required
                  rows={3}
                />
              </label>
              {validationError && <div className="inline-error" role="alert">{validationError}</div>}
              {submissionError && <div className="inline-error" role="alert">{submissionError}</div>}
              <div className="taxonomy-form-actions">
                <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
                <button type="submit" className="primary-button">Review decision</button>
              </div>
            </form>
          )}
          {pending && <div className="taxonomy-pending" role="status">Decision command is {status?.status ?? "being submitted"}…</div>}
        </div>
      </aside>
      {confirming && (
        <ConfirmationDialog
          action={action}
          proposal={gap.normalized_proposal}
          canonicalValue={action === "map_existing" ? canonicalValue : action === "accept_new" ? acceptPreview : null}
          onCancel={() => setConfirming(false)}
          onConfirm={submit}
        />
      )}
    </div>
  );
}

function ConfirmationDialog({
  action,
  proposal,
  canonicalValue,
  onCancel,
  onConfirm,
}: {
  action: DecisionAction;
  proposal: string;
  canonicalValue: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const confirmButton = useRef<HTMLButtonElement>(null);
  useEffect(() => confirmButton.current?.focus(), []);
  return (
    <div className="modal-backdrop taxonomy-confirmation-backdrop">
      <div className="modal-panel taxonomy-confirmation" role="alertdialog" aria-modal="true" aria-labelledby="taxonomy-confirm-title">
        <div className="modal-header"><h3 id="taxonomy-confirm-title">Confirm taxonomy decision</h3></div>
        <div className="modal-body">
          <p>
            {action === "reject"
              ? `Reject “${proposal}”?`
              : `${action === "accept_new" ? "Accept" : "Map"} “${proposal}” as “${canonicalValue}”?`}
          </p>
          <p className="table-subtext">This submits an audited command and may start a bounded historical backfill.</p>
          <div className="taxonomy-form-actions">
            <button type="button" className="secondary-button" onClick={onCancel}>Go back</button>
            <button ref={confirmButton} type="button" className="primary-button" onClick={onConfirm}>Confirm decision</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function TaxonomyCommandStatus({ status }: { status: ThesisBuilderTaxonomyDecisionResponse }) {
  const backfill = status.backfill;
  const commandFailure =
    status.error_code === "taxonomy_gap_conflict"
      ? "Another operator already decided this proposal. The gap list has been refreshed."
      : `Taxonomy command failed${status.error_code ? ` (${status.error_code})` : ""}. Review the decision and retry.`;
  return (
    <section className={`taxonomy-command-status ${status.status === "failed" ? "failed" : ""}`} aria-live="polite">
      <h4>Command status</h4>
      <dl className="taxonomy-gap-facts">
        <div><dt>Command</dt><dd>{status.command_id}</dd></div>
        <div><dt>Status</dt><dd>{labelToken(status.status)}</dd></div>
        {status.taxonomy_revision != null && <div><dt>Taxonomy revision</dt><dd>{status.taxonomy_revision}</dd></div>}
        {status.error_code && <div><dt>Command error</dt><dd>{labelToken(status.error_code)}</dd></div>}
      </dl>
      {status.status === "failed" && (
        <div className="inline-error" role="alert">
          {commandFailure}
        </div>
      )}
      {backfill && (
        <>
          <h4>Historical backfill</h4>
          <dl className="taxonomy-gap-facts">
            <div><dt>Status</dt><dd>{labelToken(backfill.status)}</dd></div>
            <div><dt>Matched</dt><dd>{backfill.matched_count ?? 0}</dd></div>
            <div><dt>Processed</dt><dd>{backfill.processed_count ?? 0}</dd></div>
            <div><dt>Changed</dt><dd>{backfill.changed_count ?? 0}</dd></div>
            <div><dt>Skipped</dt><dd>{backfill.skipped_count ?? 0}</dd></div>
            <div><dt>Failed</dt><dd>{backfill.failed_count ?? 0}</dd></div>
          </dl>
          {backfill.status === "failed" && (
            <div className="inline-error" role="alert">
              Historical backfill failed{backfill.error_code ? ` (${backfill.error_code})` : ""}. The taxonomy decision remains committed.
            </div>
          )}
        </>
      )}
    </section>
  );
}
