export type HealthState = "healthy" | "unhealthy";

export type DependencyHealth = {
  name: string;
  kind: "postgres" | "redis";
  state: HealthState;
  message?: string | null;
  checked_at: string;
};

export type HealthResponse = {
  readiness: HealthState;
  liveness: HealthState;
  stale_data: boolean;
  last_successful_refresh_at: string;
  dependencies: DependencyHealth[];
  active_incident_count: number;
};

export type ProviderStatus = {
  source_key: string;
  last_cycle_start_at?: string | null;
  last_cycle_end_at?: string | null;
  last_cycle_duration_seconds?: number | null;
  last_fetch_attempt_at?: string | null;
  last_non_zero_fetch_at?: string | null;
  fetch_count: number;
  fetch_error_count: number;
  dedupe_drop_count: number;
  persist_success_count: number;
  publish_success_count: number;
  last_error_code?: string | null;
  last_error_at?: string | null;
};

export type ProvidersResponse = {
  available: boolean;
  message?: string | null;
  providers: ProviderStatus[];
  generated_at: string;
};

export type ThroughputBucket = {
  window_start: string;
  source_key: string;
  fetch_count: number;
  publish_success_count: number;
  publish_error_count: number;
};

export type ThroughputResponse = {
  available: boolean;
  message?: string | null;
  window: string;
  granularity: "raw" | "hour" | "day";
  window_start_at: string;
  window_end_at: string;
  buckets: ThroughputBucket[];
  generated_at: string;
};

export type ThroughputPresetWindow = "15m" | "1h" | "1d" | "7d" | "30d";

export type ThroughputRequest =
  | { window: ThroughputPresetWindow }
  | { window: ThroughputPresetWindow; startAt: string; endAt: string };

export type ThesisBuilderEvidenceWindow = {
  window_id: number;
  ticker: string;
  exchange_code: string;
  strategy: string;
  direction?: string | null;
  window_started_at: string;
  last_evidence_at: string;
  pending_age_seconds: number;
  expires_in_seconds: number;
  evidence_count: number;
  required_evidence_count: number;
};

export type ThesisBuilderActionableCard = {
  card_id: string;
  ticker: string;
  exchange_code: string;
  strategy: string;
  direction: string;
  time_horizon: string;
  confidence: number;
  created_at: string;
  expires_at: string;
  expires_in_seconds: number;
  evidence_count: number;
  signal_published: boolean;
};

export type ThesisCardSummary = {
  card_id: string;
  ticker: string;
  exchange_code: string;
  strategy: string;
  direction: string;
  time_horizon: string;
  confidence: number;
  created_at: string;
  expires_at: string;
  expires_in_seconds: number;
  evidence_count: number;
  signal_published: boolean;
  validation_status: string;
  rejection_reason_code?: string | null;
};

export type ThesisCardListResponse = {
  available: boolean;
  message?: string | null;
  window: string;
  window_start_at: string;
  window_end_at: string;
  cards: ThesisCardSummary[];
  generated_at: string;
};

export type ThesisBuilderDeadLetterItem = {
  source_message_id: string;
  article_id: string;
  error_code?: string | null;
  failed_at: string;
};

export type WindowArticle = {
  article_id: string;
  headline: string;
  url: string;
  source: string;
  summary?: string | null;
  published_at?: string | null;
  confidence?: number | null;
  is_market_moving: boolean;
  validation_status?: string | null;
  rejection_reason_code?: string | null;
};

export type WindowArticlesResponse = {
  available: boolean;
  message?: string | null;
  window_id?: number | null;
  card_id?: string | null;
  articles: WindowArticle[];
  generated_at: string;
};

export type ThesisReprocessRequest = {
  days_back: number;
};

export type ThesisReprocessResponse = {
  run_id: string;
  status: string;
};

export type ThesisReprocessStatusResponse = {
  run_id: string;
  status: string;
  days_back: number;
  articles_found?: number | null;
  analyses_created?: number | null;
  cards_created?: number | null;
  error_code?: string | null;
  requested_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

export type ThesisBuilderConsumerHealth = {
  available: boolean;
  message?: string | null;
  stalled: boolean;
  stall_reasons: string[];
  consumer_group?: string | null;
  group_present: boolean;
  stream_length?: number | null;
  consumer_lag?: number | null;
  pending_count?: number | null;
  consumer_count?: number | null;
  min_consumer_idle_seconds?: number | null;
  oldest_consumer_idle_seconds?: number | null;
  last_analysis_age_seconds?: number | null;
  generated_at: string;
};

export type ThesisBuilderMetricsResponse = {
  available: boolean;
  message?: string | null;
  window: string;
  window_start_at: string;
  window_end_at: string;
  articles_processed_count: number;
  market_moving_articles_count: number;
  articles_included_in_cards_count: number;
  stale_articles_count: number;
  created_thesis_cards_count: number;
  pending_thesis_cards_count: number;
  oldest_pending_age_seconds?: number | null;
  average_pending_age_seconds?: number | null;
  minimum_pending_expires_in_seconds?: number | null;
  average_pending_expires_in_seconds?: number | null;
  missed_stale_thesis_cards_count: number;
  stale_evidence_exceeded_avg_seconds?: number | null;
  stale_evidence_exceeded_p95_seconds?: number | null;
  stale_evidence_exceeded_max_seconds?: number | null;
  dead_letter_count: number;
  recent_dead_letters: ThesisBuilderDeadLetterItem[];
  pending_windows: ThesisBuilderEvidenceWindow[];
  actionable_cards: ThesisBuilderActionableCard[];
  consumer_health?: ThesisBuilderConsumerHealth | null;
  generated_at: string;
};

export type ThesisBuilderAlreadyPricedGateConfig = {
  strategy: string;
  atr_multiple: number;
  return_threshold: number;
};

export type ThesisBuilderConfigResponse = {
  already_priced_gate: ThesisBuilderAlreadyPricedGateConfig[];
  generated_at: string;
};

export type ThesisBuilderThroughputBucket = {
  window_start: string;
  consumed_messages_count: number;
  skipped_messages_count: number;
  failed_messages_count: number;
  processed_articles_count: number;
  news_catalyst_articles_count: number;
  market_moving_articles_count: number;
};

export type ThesisBuilderThroughputResponse = {
  available: boolean;
  message?: string | null;
  window: string;
  granularity: "raw" | "hour" | "day";
  window_start_at: string;
  window_end_at: string;
  buckets: ThesisBuilderThroughputBucket[];
  generated_at: string;
};

export type BacklogResponse = {
  available: boolean;
  message?: string | null;
  pending_count: number;
  retrying_count: number;
  dead_letter_count: number;
  max_attempt_age_seconds?: number | null;
  generated_at: string;
};

export type DeadLetterItem = {
  obligation_id: string;
  source_key: string;
  canonical_event_id: string;
  reason?: string | null;
  first_failure_at?: string | null;
  updated_at: string;
};

export type DeadLetterResponse = {
  available: boolean;
  message?: string | null;
  items: DeadLetterItem[];
  limit: number;
  offset: number;
  generated_at: string;
};

export type FilterQualityRunStatus = "running" | "completed" | "failed";

export type FilterQualityRunSummary = {
  run_id: string;
  status: FilterQualityRunStatus;
  news_window_start_at: string;
  news_window_end_at: string;
  created_at: string;
  started_at: string;
  finished_at?: string | null;
  error_code?: string | null;
  rejection_precision_proxy?: number | null;
  incorrectly_accepted_rate_estimate?: number | null;
  total_filter_quality?: number | null;
  total_correct_count: number;
  assumed_correct_accepted_count: number;
  evaluation_subject: string;
  evaluated_filter_config?: NewsFilterConfigPayload | null;
  dataset_input_count: number;
  dataset_rejected_count: number;
  dataset_accepted_count: number;
  rejected_items_evaluated: number;
  accepted_items_sampled: number;
  correctly_rejected_count: number;
  incorrectly_rejected_count: number;
  correctly_accepted_count: number;
  incorrectly_accepted_count: number;
  item_failed_count: number;
  item_error_codes: Record<string, number>;
  summary_json: Record<string, unknown>;
  recommendation_summary_md: string;
};

export type FilterQualityStatusResponse = {
  available: boolean;
  message?: string | null;
  running_run?: FilterQualityRunSummary | null;
  last_run?: FilterQualityRunSummary | null;
  generated_at: string;
};

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export type FilterQualityIncorrectlyRejectedItem = {
  assessment_id: string;
  run_id: string;
  article_id: string;
  headline: string;
  summary?: string | null;
  url: string;
  source: string;
  published_at: string;
  production_matched_article_id?: string | null;
  production_matched_article_headline?: string | null;
  production_matched_article_url?: string | null;
  production_matched_article_source?: string | null;
  production_matched_article_published_at?: string | null;
  simulation_matched_article_id?: string | null;
  simulation_matched_article_headline?: string | null;
  simulation_matched_article_url?: string | null;
  simulation_matched_article_source?: string | null;
  simulation_matched_article_published_at?: string | null;
  production_filter_outcome?: string | null;
  simulation_filter_outcome?: string | null;
  rejection_reason_code?: string | null;
  production_rejection_reason_code?: string | null;
  simulation_rejection_reason_code?: string | null;
  probable_cause?: string | null;
  improvement_suggestion?: string | null;
  rationale?: string | null;
  classification_confidence?: number | null;
  suggestion_json: Record<string, unknown>;
  recommended_include_keywords: string[];
  evaluated_at: string;
};

export type FilterQualityIncorrectlyRejectedResponse = {
  run_id: string;
  items: FilterQualityIncorrectlyRejectedItem[];
  generated_at: string;
};

export type NewsAnalysisItem = {
  id: number;
  article_id: string;
  ticker: string;
  exchange_code: string;
  analyzed_at: string;
  content_type?: string | null;
  validation_status: string;
  rejection_reason_code?: string | null;
  confidence: number;
  is_market_moving: boolean;
  direction?: string | null;
  strategy?: string | null;
  reasoning?: string | null;
  headline?: string | null;
  summary?: string | null;
  url?: string | null;
  source?: string | null;
};

export type NewsAnalysesResponse = {
  available: boolean;
  message?: string | null;
  items: NewsAnalysisItem[];
  has_more: boolean;
  generated_at: string;
};

export type FetchedArticle = {
  article_id: string;
  source: string;
  source_key: string;
  headline: string;
  summary?: string | null;
  url: string;
  tickers: string[];
  published_at: string;
  fetched_at: string;
  filter_outcome?: string | null;
  rejection_reason_code?: string | null;
  matched_article_id?: string | null;
};

export type FetchedArticlesResponse = {
  available: boolean;
  message?: string | null;
  window?: string | null;
  items: FetchedArticle[];
  generated_at: string;
};

export type FilterQualityIncorrectlyAcceptedItem = {
  assessment_id: string;
  run_id: string;
  article_id: string;
  headline: string;
  summary?: string | null;
  url: string;
  source: string;
  published_at: string;
  production_filter_outcome?: string | null;
  simulation_filter_outcome?: string | null;
  probable_cause?: string | null;
  improvement_suggestion?: string | null;
  rationale?: string | null;
  classification_confidence?: number | null;
  suggestion_json: Record<string, unknown>;
  evaluated_at: string;
};

export type FilterQualityIncorrectlyAcceptedResponse = {
  run_id: string;
  items: FilterQualityIncorrectlyAcceptedItem[];
  generated_at: string;
};

export type FilterQualityStartRunResponse = {
  run_id: string;
  status: "running";
};

export type FilterQualityStartRunRequest = {
  accepted_audit_enabled?: boolean;
};

export type NewsFilterConfigPayload = {
  filter_config_id?: string | null;
  config_name: string;
  config_role: string;
  status: string;
  include_keywords: string[];
  exclude_keywords: string[];
  watchlist_tickers: string[];
  dedupe_algorithm: string;
  dedupe_similarity_threshold: number;
  dedupe_lookback_hours: number;
  created_from_run_id?: string | null;
};

export type FilterConfigSimulationStartResponse = {
  run_id: string;
  status: "running";
};

export type WatchlistItemPayload = {
  ticker: string;
  exchange_code: string;
  display_name: string;
  aliases: string[];
  source?: string;
};

export type WatchlistItemResponse = {
  ticker: string;
  exchange_code: string;
  display_name?: string | null;
  aliases: string[];
  is_active: boolean;
  source: string;
  has_missing_aliases: boolean;
};

export type WatchlistResponse = {
  lookup_providers_configured: boolean;
  lookup_message?: string | null;
  items: WatchlistItemResponse[];
  generated_at: string;
};

export type WatchlistLookupSuggestionResponse = {
  ticker: string;
  exchange_code: string;
  display_name: string;
  aliases: string[];
  provider: string;
};

export type WatchlistLookupResponse = {
  query: string;
  lookup_providers_configured: boolean;
  lookup_message?: string | null;
  suggestions: WatchlistLookupSuggestionResponse[];
  cached: boolean;
  provider_warnings?: string[];
  generated_at: string;
};

export type AliasDiscoveryResponse = {
  ticker: string;
  exchange_code: string;
  display_name?: string | null;
  aliases: string[];
  provider?: string | null;
  found: boolean;
  cached: boolean;
  generated_at: string;
};

export type BacktestRunStatus = "running" | "completed" | "failed";

export type BacktestMode = "replay" | "regeneration";

export type BacktestTimingScenario = "ideal" | "actual" | "both";

export type BacktestCardPopulation = "all" | "approved_only" | "rejected_only";

export type BacktestRunProgress = {
  phase: "prewarming" | "regenerating" | "simulating" | string;
  done: number;
  total: number;
  current_ticker?: string | null;
  updated_at: string;
};

export type BacktestRunSummary = {
  run_id: string;
  status: BacktestRunStatus;
  window_start_at: string;
  window_end_at: string;
  mode: string;
  timing_scenario: string;
  card_population: string;
  strategies_requested?: string[] | null;
  initial_capital: number;
  llm_model?: string | null;
  net_pnl?: number | null;
  total_return?: number | null;
  win_rate?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_code?: string | null;
  progress?: BacktestRunProgress | null;
};

export type BacktestsResponse = {
  available: boolean;
  message?: string | null;
  window: string;
  runs: BacktestRunSummary[];
  active_run?: BacktestRunSummary | null;
  generated_at: string;
};

export type BacktestMetrics = {
  net_pnl?: number | null;
  gross_profit?: number | null;
  gross_loss?: number | null;
  total_commission?: number | null;
  total_slippage?: number | null;
  total_return?: number | null;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  expectancy?: number | null;
  max_drawdown?: number | null;
  max_drawdown_duration_seconds?: number | null;
  sharpe_ratio?: number | null;
  exposure_fraction?: number | null;
  signal_accuracy?: number | null;
  cards_considered?: number | null;
  cards_in_population?: number | null;
  cards_live_executable?: number | null;
  cards_skipped_no_price?: number | null;
  trades_opened?: number | null;
  trades_closed?: number | null;
  trades_risk_blocked?: number | null;
};

export type BacktestStrategyMetrics = {
  strategy: string;
  trades_opened?: number | null;
  trades_closed?: number | null;
  trades_risk_blocked?: number | null;
  net_pnl?: number | null;
  gross_profit?: number | null;
  gross_loss?: number | null;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  expectancy?: number | null;
};

export type BacktestCardStatusBucket = {
  bucket: string;
  trades_opened?: number | null;
  trades_closed?: number | null;
  trades_risk_blocked?: number | null;
  net_pnl?: number | null;
  gross_profit?: number | null;
  gross_loss?: number | null;
  win_rate?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  profit_factor?: number | null;
  expectancy?: number | null;
};

export type BacktestDelays = {
  avg_news_fetch_delay_seconds?: number | null;
  p95_news_fetch_delay_seconds?: number | null;
  max_news_fetch_delay_seconds?: number | null;
  avg_thesis_build_delay_seconds?: number | null;
  p95_thesis_build_delay_seconds?: number | null;
  max_thesis_build_delay_seconds?: number | null;
  avg_total_pipeline_delay_seconds?: number | null;
  p95_total_pipeline_delay_seconds?: number | null;
  max_total_pipeline_delay_seconds?: number | null;
};

export type BacktestGap = {
  pnl_gap?: number | null;
  win_rate_gap?: number | null;
  trades_flipped_by_delay?: number | null;
};

export type BacktestRegenerationStats = {
  articles_found?: number | null;
  articles_relevant?: number | null;
  articles_analyzed?: number | null;
  analyses_created?: number | null;
  cards_created?: number | null;
  evidence_windows_created?: number | null;
  budget_exhausted?: boolean | null;
};

export type BacktestDetailResponse = {
  available: boolean;
  message?: string | null;
  run: BacktestRunSummary;
  metrics: BacktestMetrics;
  per_strategy: BacktestStrategyMetrics[];
  card_status_breakdown: BacktestCardStatusBucket[];
  delays: BacktestDelays;
  gap?: BacktestGap | null;
  regeneration?: BacktestRegenerationStats | null;
  generated_at: string;
};

export type BacktestTrade = {
  trade_id: string;
  ticker: string;
  exchange_code: string;
  strategy: string;
  direction: string;
  entry_timing_scenario: string;
  entry_at: string;
  entry_price?: number | null;
  exit_at?: string | null;
  exit_price?: number | null;
  net_pnl?: number | null;
  return_pct?: number | null;
  exit_reason?: string | null;
  risk_block_rule?: string | null;
  news_fetch_delay_seconds?: number | null;
  thesis_build_delay_seconds?: number | null;
  total_pipeline_delay_seconds?: number | null;
  card_decision_state?: string | null;
  card_was_live_expired?: boolean | null;
};

export type BacktestTradesResponse = {
  available: boolean;
  message?: string | null;
  run_id: string;
  trades: BacktestTrade[];
  limit: number;
  offset: number;
  total_count: number;
  generated_at: string;
};

export type BacktestTradeFilters = {
  timing_scenario?: string;
  strategy?: string;
  exit_reason?: string;
  card_status?: string;
  limit?: number;
  offset?: number;
};

export type BacktestEquityPoint = {
  as_of: string;
  equity?: number | null;
  open_positions?: number | null;
};

export type BacktestEquitySeries = {
  timing_scenario: "ideal" | "actual";
  points: BacktestEquityPoint[];
};

export type BacktestEquityResponse = {
  available: boolean;
  message?: string | null;
  run_id: string;
  series: BacktestEquitySeries[];
  generated_at: string;
};

export type BacktestCardTrade = {
  trade_id: string;
  entry_timing_scenario: string;
  entry_at: string | null;
  entry_price: number | null;
  exit_at: string | null;
  exit_price: number | null;
  net_pnl: number | null;
  return_pct: number | null;
  exit_reason: string | null;
  risk_block_rule: string | null;
};

export type BacktestCard = {
  thesis_card_id: string;
  ticker: string;
  exchange_code: string;
  direction: string;
  strategy: string;
  time_horizon: string | null;
  confidence: number | null;
  decision_state: string;
  card_created_at: string;
  card_expires_at: string | null;
  trades: BacktestCardTrade[];
};

export type BacktestCardsResponse = {
  available: boolean;
  message?: string | null;
  run_id: string;
  cards: BacktestCard[];
  generated_at: string;
};

export type StartBacktestRequest = {
  window_start_at: string;
  window_end_at: string;
  mode: BacktestMode;
  timing_scenario: BacktestTimingScenario;
  card_population: BacktestCardPopulation;
  strategies?: string[] | null;
  initial_capital?: number | null;
  run_note?: string | null;
  llm_model?: string | null;
  required_evidence_count?: number | null;
  evidence_collection_max_minutes?: number | null;
};

export type StartBacktestResponse = {
  run_id: string;
  status: "running";
};

const apiBaseUrl = import.meta.env.VITE_UI_API_BASE_URL ?? "";

function apiUrl(path: string): string {
  const normalizedBase = apiBaseUrl.replace(/\/$/, "");
  if (!normalizedBase) {
    return path;
  }
  if (normalizedBase.endsWith("/api") && path.startsWith("/api/")) {
    return `${normalizedBase}${path.slice(4)}`;
  }
  return `${normalizedBase}${path}`;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path));
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (typeof detail === "string" && detail.trim()) {
        message = detail;
      }
    } catch {
      // Keep the HTTP status fallback when the server returns a non-JSON body.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (typeof detail === "string" && detail.trim()) {
        message = detail;
      }
      if (detail?.message && detail?.run_id) {
        message = `${detail.message}: ${detail.run_id}`;
      }
    } catch {
      // Keep the HTTP status fallback when the server returns a non-JSON body.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (typeof detail === "string" && detail.trim()) {
        message = detail;
      }
    } catch {
      // Keep the HTTP status fallback when the server returns a non-JSON body.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

async function deleteJson(path: string): Promise<void> {
  const response = await fetch(apiUrl(path), { method: "DELETE" });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (typeof detail === "string" && detail.trim()) {
        message = detail;
      }
    } catch {
      // Keep the HTTP status fallback when the server returns a non-JSON body.
    }
    throw new ApiError(message, response.status);
  }
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/health");
}

export function fetchProviders(): Promise<ProvidersResponse> {
  return getJson<ProvidersResponse>("/api/providers");
}

export function fetchThroughput(request: ThroughputRequest): Promise<ThroughputResponse> {
  if (!("startAt" in request)) {
    return getJson<ThroughputResponse>(`/api/metrics/throughput?window=${encodeURIComponent(request.window)}`);
  }
  const params = new URLSearchParams({
    window: request.window,
    start_at: request.startAt,
    end_at: request.endAt
  });
  return getJson<ThroughputResponse>(`/api/metrics/throughput?${params.toString()}`);
}

export function fetchThesisBuilderMetrics(window: ThroughputPresetWindow): Promise<ThesisBuilderMetricsResponse> {
  return getJson<ThesisBuilderMetricsResponse>(
    `/api/thesis-builder/metrics?window=${encodeURIComponent(window)}`
  );
}

export function fetchThesisBuilderThroughput(
  window: ThroughputPresetWindow
): Promise<ThesisBuilderThroughputResponse> {
  return getJson<ThesisBuilderThroughputResponse>(
    `/api/thesis-builder/throughput?window=${encodeURIComponent(window)}`
  );
}

export function fetchThesisBuilderConfig(): Promise<ThesisBuilderConfigResponse> {
  return getJson<ThesisBuilderConfigResponse>("/api/thesis-builder/config");
}

export function fetchThesisCards(window: ThroughputPresetWindow): Promise<ThesisCardListResponse> {
  return getJson<ThesisCardListResponse>(
    `/api/thesis-builder/cards?window=${encodeURIComponent(window)}`
  );
}

export function fetchWindowArticles(windowId: number): Promise<WindowArticlesResponse> {
  return getJson<WindowArticlesResponse>(
    `/api/thesis-builder/windows/${encodeURIComponent(windowId)}/articles`
  );
}

export function fetchNewsAnalyses(params: { window: ThroughputPresetWindow; limit?: number }): Promise<NewsAnalysesResponse> {
  const p = new URLSearchParams({ window: params.window });
  if (params.limit != null) p.set("limit", String(params.limit));
  return getJson<NewsAnalysesResponse>(`/api/thesis-builder/analyses?${p.toString()}`);
}

export function fetchFetchedArticles(params: { window: ThroughputPresetWindow; limit?: number }): Promise<FetchedArticlesResponse> {
  const p = new URLSearchParams({ window: params.window });
  if (params.limit != null) p.set("limit", String(params.limit));
  return getJson<FetchedArticlesResponse>(`/api/news-fetcher/fetched-articles?${p.toString()}`);
}

export function fetchThesisCardArticles(cardId: string): Promise<WindowArticlesResponse> {
  return getJson<WindowArticlesResponse>(
    `/api/thesis-builder/cards/${encodeURIComponent(cardId)}/articles`
  );
}

export function reprocessThesisBuilder(payload: ThesisReprocessRequest): Promise<ThesisReprocessResponse> {
  return postJson<ThesisReprocessResponse>("/api/thesis-builder/reprocess", payload);
}

export function fetchReprocessStatus(runId: string): Promise<ThesisReprocessStatusResponse> {
  return getJson<ThesisReprocessStatusResponse>(
    `/api/thesis-builder/reprocess/${encodeURIComponent(runId)}`
  );
}

export function fetchBacklog(): Promise<BacklogResponse> {
  return getJson<BacklogResponse>("/api/backlog");
}

export function fetchDeadLetters(): Promise<DeadLetterResponse> {
  return getJson<DeadLetterResponse>("/api/dead-letter?limit=25&offset=0");
}

export function fetchFilterQualityStatus(): Promise<FilterQualityStatusResponse> {
  return getJson<FilterQualityStatusResponse>("/api/filter-quality");
}

export function fetchFilterQualityIncorrectlyRejected(
  runId: string
): Promise<FilterQualityIncorrectlyRejectedResponse> {
  return getJson<FilterQualityIncorrectlyRejectedResponse>(
    `/api/filter-quality/runs/${encodeURIComponent(runId)}/incorrectly-rejected`
  );
}

export function fetchFilterQualityIncorrectlyAccepted(
  runId: string
): Promise<FilterQualityIncorrectlyAcceptedResponse> {
  return getJson<FilterQualityIncorrectlyAcceptedResponse>(
    `/api/filter-quality/runs/${encodeURIComponent(runId)}/incorrectly-accepted`
  );
}

export function startFilterQualityRun(
  payload?: FilterQualityStartRunRequest
): Promise<FilterQualityStartRunResponse> {
  return postJson<FilterQualityStartRunResponse>("/api/filter-quality/runs", payload);
}

export function fetchProductionFilterConfig(): Promise<NewsFilterConfigPayload> {
  return getJson<NewsFilterConfigPayload>("/api/filter-configs/production");
}

export function fetchTestFilterConfig(): Promise<NewsFilterConfigPayload> {
  return getJson<NewsFilterConfigPayload>("/api/filter-configs/test");
}

export function saveTestFilterConfig(config: NewsFilterConfigPayload): Promise<NewsFilterConfigPayload> {
  return putJson<NewsFilterConfigPayload>("/api/filter-configs/test", config);
}

export function runTestFilterSimulation(): Promise<FilterConfigSimulationStartResponse> {
  return postJson<FilterConfigSimulationStartResponse>("/api/filter-configs/test/simulations");
}

export function promoteTestFilterConfig(): Promise<NewsFilterConfigPayload> {
  return postJson<NewsFilterConfigPayload>("/api/filter-configs/test/promote");
}

export function fetchWatchlist(): Promise<WatchlistResponse> {
  return getJson<WatchlistResponse>("/api/watchlist");
}

export function lookupWatchlist(query: string, expand = false): Promise<WatchlistLookupResponse> {
  const params = new URLSearchParams({ query });
  if (expand) params.set("expand", "true");
  return getJson<WatchlistLookupResponse>(`/api/watchlist/lookups?${params}`);
}

export function addWatchlistItem(payload: WatchlistItemPayload): Promise<WatchlistItemResponse> {
  return postJson<WatchlistItemResponse>("/api/watchlist", payload);
}

export function updateWatchlistItem(
  ticker: string,
  exchangeCode: string,
  payload: WatchlistItemPayload
): Promise<WatchlistItemResponse> {
  return putJson<WatchlistItemResponse>(
    `/api/watchlist/${encodeURIComponent(ticker)}/${encodeURIComponent(exchangeCode)}`,
    payload
  );
}

export function discoverWatchlistAliases(
  ticker: string,
  exchangeCode: string
): Promise<AliasDiscoveryResponse> {
  return postJson<AliasDiscoveryResponse>(
    `/api/watchlist/${encodeURIComponent(ticker)}/${encodeURIComponent(exchangeCode)}/alias-discovery`
  );
}

export function deactivateWatchlistItem(ticker: string, exchangeCode: string): Promise<void> {
  return deleteJson(`/api/watchlist/${encodeURIComponent(ticker)}/${encodeURIComponent(exchangeCode)}`);
}

export function fetchBacktests(window: ThroughputPresetWindow): Promise<BacktestsResponse> {
  return getJson<BacktestsResponse>(`/api/backtests?window=${encodeURIComponent(window)}`);
}

export function fetchBacktestDetail(runId: string): Promise<BacktestDetailResponse> {
  return getJson<BacktestDetailResponse>(`/api/backtests/${encodeURIComponent(runId)}`);
}

export function fetchBacktestTrades(
  runId: string,
  filters: BacktestTradeFilters = {}
): Promise<BacktestTradesResponse> {
  const params = new URLSearchParams();
  if (filters.timing_scenario) params.set("timing_scenario", filters.timing_scenario);
  if (filters.strategy) params.set("strategy", filters.strategy);
  if (filters.exit_reason) params.set("exit_reason", filters.exit_reason);
  if (filters.card_status) params.set("card_status", filters.card_status);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return getJson<BacktestTradesResponse>(
    `/api/backtests/${encodeURIComponent(runId)}/trades?${params.toString()}`
  );
}

export function fetchBacktestEquity(runId: string): Promise<BacktestEquityResponse> {
  return getJson<BacktestEquityResponse>(`/api/backtests/${encodeURIComponent(runId)}/equity`);
}

export function fetchBacktestCards(runId: string): Promise<BacktestCardsResponse> {
  return getJson<BacktestCardsResponse>(`/api/backtests/${encodeURIComponent(runId)}/cards`);
}

export function startBacktest(request: StartBacktestRequest): Promise<StartBacktestResponse> {
  return postJson<StartBacktestResponse>("/api/backtests", request);
}
