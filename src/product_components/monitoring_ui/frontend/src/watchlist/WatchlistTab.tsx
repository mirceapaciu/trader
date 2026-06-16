import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addWatchlistItem,
  deactivateWatchlistItem,
  discoverWatchlistAliases,
  fetchWatchlist,
  lookupWatchlist,
  updateWatchlistItem,
  type AliasDiscoveryResponse,
  type WatchlistItemPayload,
  type WatchlistItemResponse,
  type WatchlistLookupSuggestionResponse
} from "../api";

const EMPTY_FORM: WatchlistItemPayload = {
  ticker: "",
  exchange_code: "",
  display_name: "",
  aliases: [],
  source: "manual"
};

export function WatchlistTab() {
  const queryClient = useQueryClient();
  const [searchText, setSearchText] = useState("");
  const [selectedSuggestion, setSelectedSuggestion] = useState<WatchlistLookupSuggestionResponse | null>(null);
  const [manualForm, setManualForm] = useState<WatchlistItemPayload>(EMPTY_FORM);
  const [manualAliasesText, setManualAliasesText] = useState("");
  const [repairTarget, setRepairTarget] = useState<WatchlistItemResponse | null>(null);
  const [repairAliasesText, setRepairAliasesText] = useState("");
  const watchlist = useQuery({
    queryKey: ["watchlist"],
    queryFn: fetchWatchlist
  });
  const lookupProvidersConfigured = watchlist.data?.lookup_providers_configured !== false;
  const normalizedSearchText = normalizeLookupQuery(searchText);
  const deferredSearchText = useDeferredValue(normalizedSearchText);
  const lookup = useQuery({
    queryKey: ["watchlist", "lookup", deferredSearchText],
    queryFn: () => lookupWatchlist(deferredSearchText),
    enabled: lookupProvidersConfigured && deferredSearchText.length >= 2
  });

  const addMutation = useMutation({
    mutationFn: addWatchlistItem,
    onSuccess: () => {
      setSelectedSuggestion(null);
      setSearchText("");
      setManualForm(EMPTY_FORM);
      setManualAliasesText("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    }
  });
  const updateMutation = useMutation({
    mutationFn: ({ ticker, exchangeCode, payload }: { ticker: string; exchangeCode: string; payload: WatchlistItemPayload }) =>
      updateWatchlistItem(ticker, exchangeCode, payload),
    onSuccess: () => {
      setRepairTarget(null);
      setRepairAliasesText("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    }
  });
  const deactivateMutation = useMutation({
    mutationFn: ({ ticker, exchangeCode }: { ticker: string; exchangeCode: string }) =>
      deactivateWatchlistItem(ticker, exchangeCode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] })
  });
  const aliasDiscoveryMutation = useMutation({
    mutationFn: ({ ticker, exchangeCode }: { ticker: string; exchangeCode: string }) =>
      discoverWatchlistAliases(ticker, exchangeCode),
    onSuccess: (result) => {
      if (result.found) {
        setRepairAliasesText(result.aliases.join(", "));
      }
    }
  });

  useEffect(() => {
    if (!selectedSuggestion) {
      return;
    }
    setManualForm({
      ticker: selectedSuggestion.ticker,
      exchange_code: selectedSuggestion.exchange_code,
      display_name: selectedSuggestion.display_name,
      aliases: [...selectedSuggestion.aliases],
      source: "manual"
    });
    setManualAliasesText(selectedSuggestion.aliases.join(", "));
  }, [selectedSuggestion]);

  useEffect(() => {
    setSelectedSuggestion(null);
  }, [deferredSearchText]);

  const suggestions = lookup.data?.suggestions ?? [];
  const lookupStatus = useMemo(() => {
    if (!lookupProvidersConfigured) {
      return watchlist.data?.lookup_message ?? "Ticker lookup providers are not configured. Manual add is still available.";
    }
    if (deferredSearchText.length < 2) {
      return "Type at least two characters to search tickers.";
    }
    if (lookup.isLoading) {
      return "Looking up symbols...";
    }
    if (lookup.isError) {
      return "Lookup failed. You can still add the instrument manually below.";
    }
    if (suggestions.length === 0) {
      return "No provider suggestions found. Manual add is available.";
    }
    return lookup.data?.cached ? "Showing cached lookup suggestions." : "Showing provider suggestions.";
  }, [
    deferredSearchText.length,
    lookup.data?.cached,
    lookup.isError,
    lookup.isLoading,
    lookupProvidersConfigured,
    suggestions.length,
    watchlist.data?.lookup_message,
  ]);

  return (
    <main className="shell watchlist-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Trader</p>
          <h1>Watchlist Admin</h1>
        </div>
        <div className="timestamp">
          Watchlist rows
          <strong>{watchlist.data?.items.length ?? 0}</strong>
        </div>
      </header>

      <section className="watchlist-layout">
        <article className="panel panel-large">
          <div className="panel-heading">
            <div>
              <h2>Current watchlist</h2>
              <span>Shared watchlist membership with alias completeness warnings.</span>
            </div>
          </div>
          {watchlist.isLoading ? <p>Loading watchlist...</p> : null}
          {watchlist.isError ? <p>Unable to load watchlist right now.</p> : null}
          <div className="watchlist-list">
            {(watchlist.data?.items ?? []).map((item) => (
              <article key={`${item.ticker}:${item.exchange_code}`} className="watchlist-card">
                <div className="watchlist-card-header">
                  <div>
                    <strong>{item.ticker}:{item.exchange_code}</strong>
                    <span>{item.display_name || "Unnamed instrument"}</span>
                  </div>
                  <button
                    type="button"
                    className="danger-button"
                    onClick={() => deactivateMutation.mutate({ ticker: item.ticker, exchangeCode: item.exchange_code })}
                    disabled={deactivateMutation.isPending}
                  >
                    Deactivate
                  </button>
                </div>
                <div className="chip-row">
                  {item.aliases.length > 0 ? item.aliases.map((alias) => <span key={alias} className="chip">{alias}</span>) : <span className="chip warning">No aliases</span>}
                </div>
                {item.has_missing_aliases ? (
                  <div className="warning-row">
                    <span className="warning-text">This ticker has no aliases and may miss company-name matches in news.</span>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => {
                        setRepairTarget(item);
                        setRepairAliasesText(item.aliases.join(", "));
                        aliasDiscoveryMutation.mutate({ ticker: item.ticker, exchangeCode: item.exchange_code });
                      }}
                    >
                      Auto-find aliases
                    </button>
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </article>

        <aside className="watchlist-side">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Add ticker</h2>
                <span>Search first, then review aliases before saving.</span>
              </div>
            </div>
            <label className="stacked-field">
              Search ticker or company name
              <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="NVIDIA, NVDA, Rheinmetall..." />
            </label>
            {!lookupProvidersConfigured ? (
              <div className="inline-warning compact">
                {watchlist.data?.lookup_message ?? "Ticker lookup providers are not configured. Manual add is still available."}
              </div>
            ) : null}
            {normalizedSearchText && normalizedSearchText !== searchText.trim() ? (
              <p className="helper-text">Searching for: <strong>{normalizedSearchText}</strong></p>
            ) : null}
            <p className="helper-text">{lookupStatus}</p>
            <div className="watchlist-suggestions">
              {suggestions.map((suggestion) => (
                <button
                  key={`${suggestion.ticker}:${suggestion.exchange_code}`}
                  type="button"
                  className={selectedSuggestion?.ticker === suggestion.ticker && selectedSuggestion.exchange_code === suggestion.exchange_code ? "suggestion-button active" : "suggestion-button"}
                  onClick={() => setSelectedSuggestion(suggestion)}
                >
                  <strong>{suggestion.ticker}:{suggestion.exchange_code}</strong>
                  <span>{suggestion.display_name}</span>
                </button>
              ))}
            </div>
            {selectedSuggestion ? (
              <div className="lookup-preview" data-testid="watchlist-preview">
                <strong>Selected instrument</strong>
                <span>{selectedSuggestion.display_name}</span>
                <div className="chip-row">
                  {selectedSuggestion.aliases.map((alias) => <span key={alias} className="chip">{alias}</span>)}
                </div>
                <button type="button" className="primary-button" onClick={() => addMutation.mutate(selectedSuggestion)} disabled={addMutation.isPending}>
                  {addMutation.isPending ? "Saving" : "Add to watchlist"}
                </button>
              </div>
            ) : null}
          </section>

          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Manual add</h2>
                <span>Use when provider lookup is missing or incomplete.</span>
              </div>
            </div>
            <ManualWatchlistForm
              form={manualForm}
              aliasesText={manualAliasesText}
              onFormChange={setManualForm}
              onAliasesChange={setManualAliasesText}
              onSubmit={() =>
                addMutation.mutate({
                  ...manualForm,
                  aliases: normalizeList(manualAliasesText),
                  source: "manual"
                })
              }
              submitLabel={addMutation.isPending ? "Saving" : "Add manually"}
            />
          </section>

          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Alias repair</h2>
                <span>Repair warning rows automatically or enter aliases manually.</span>
              </div>
            </div>
            {repairTarget ? (
              <>
                <p className="helper-text">
                  Repairing <strong>{repairTarget.ticker}:{repairTarget.exchange_code}</strong>
                  {aliasDiscoveryMutation.data?.provider ? ` via ${aliasDiscoveryMutation.data.provider}` : ""}
                  {aliasDiscoveryMutation.data?.cached ? " (cached)" : ""}
                </p>
                {renderAliasDiscoveryStatus(aliasDiscoveryMutation.data, aliasDiscoveryMutation.isPending)}
                <ManualWatchlistForm
                  form={{
                    ticker: repairTarget.ticker,
                    exchange_code: repairTarget.exchange_code,
                    display_name: repairTarget.display_name || repairTarget.ticker,
                    aliases: repairTarget.aliases,
                    source: repairTarget.source
                  }}
                  aliasesText={repairAliasesText}
                  onFormChange={() => undefined}
                  onAliasesChange={setRepairAliasesText}
                  onSubmit={() =>
                    updateMutation.mutate({
                      ticker: repairTarget.ticker,
                      exchangeCode: repairTarget.exchange_code,
                      payload: {
                        ticker: repairTarget.ticker,
                        exchange_code: repairTarget.exchange_code,
                        display_name: repairTarget.display_name || repairTarget.ticker,
                        aliases: normalizeList(repairAliasesText),
                        source: repairTarget.source
                      }
                    })
                  }
                  submitLabel={updateMutation.isPending ? "Saving" : "Save aliases"}
                  lockIdentity
                />
              </>
            ) : (
              <p className="helper-text">Select a warning row to repair aliases.</p>
            )}
          </section>
        </aside>
      </section>
    </main>
  );
}

function ManualWatchlistForm({
  form,
  aliasesText,
  onFormChange,
  onAliasesChange,
  onSubmit,
  submitLabel,
  lockIdentity = false
}: {
  form: WatchlistItemPayload;
  aliasesText: string;
  onFormChange: (value: WatchlistItemPayload) => void;
  onAliasesChange: (value: string) => void;
  onSubmit: () => void;
  submitLabel: string;
  lockIdentity?: boolean;
}) {
  return (
    <div className="manual-form">
      <div className="manual-grid">
        <label className="stacked-field">
          Ticker
          <input
            value={form.ticker}
            onChange={(event) => onFormChange({ ...form, ticker: event.target.value.toUpperCase() })}
            disabled={lockIdentity}
          />
        </label>
        <label className="stacked-field">
          Market code
          <input
            value={form.exchange_code}
            onChange={(event) => onFormChange({ ...form, exchange_code: event.target.value.toUpperCase() })}
            disabled={lockIdentity}
          />
        </label>
      </div>
      <label className="stacked-field">
        Display name
        <input
          value={form.display_name}
          onChange={(event) => onFormChange({ ...form, display_name: event.target.value })}
          disabled={lockIdentity}
        />
      </label>
      <label className="stacked-field">
        Aliases
        <textarea value={aliasesText} onChange={(event) => onAliasesChange(event.target.value)} />
      </label>
      <button type="button" className="primary-button" onClick={onSubmit}>
        {submitLabel}
      </button>
    </div>
  );
}

function renderAliasDiscoveryStatus(data: AliasDiscoveryResponse | undefined, pending: boolean) {
  if (pending) {
    return <p className="helper-text">Searching for aliases...</p>;
  }
  if (!data) {
    return null;
  }
  if (!data.found) {
    return <p className="helper-text">No provider aliases found. Manual entry is still available.</p>;
  }
  return (
    <div className="lookup-preview">
      <strong>Discovered aliases</strong>
      <div className="chip-row">
        {data.aliases.map((alias) => <span key={alias} className="chip">{alias}</span>)}
      </div>
    </div>
  );
}

function normalizeList(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split(",")
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean)
    )
  );
}

function normalizeLookupQuery(value: string): string {
  return value
    .trim()
    .replace(/[\u2010-\u2015_-]+/g, " ")
    .replace(/[.,/]+/g, " ")
    .replace(/\s+/g, " ");
}
