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
  fetch_count: number;
  fetch_error_count: number;
  dedupe_drop_count: number;
  persist_success_count: number;
  publish_success_count: number;
  last_error_code?: string | null;
  last_error_at?: string | null;
};

export type ProvidersResponse = {
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
  window: string;
  buckets: ThroughputBucket[];
  generated_at: string;
};

export type BacklogResponse = {
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
  running_run?: FilterQualityRunSummary | null;
  last_run?: FilterQualityRunSummary | null;
  generated_at: string;
};

export type FilterQualityIncorrectlyRejectedItem = {
  assessment_id: string;
  run_id: string;
  article_id: string;
  headline: string;
  url: string;
  source: string;
  published_at: string;
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

export type FilterQualityStartRunResponse = {
  run_id: string;
  status: "running";
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
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

async function postJson<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path), { method: "POST" });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (detail?.message && detail?.run_id) {
        message = `${detail.message}: ${detail.run_id}`;
      }
    } catch {
      // Keep the HTTP status fallback when the server returns a non-JSON body.
    }
    throw new Error(message);
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
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/health");
}

export function fetchProviders(): Promise<ProvidersResponse> {
  return getJson<ProvidersResponse>("/api/providers");
}

export function fetchThroughput(window: string): Promise<ThroughputResponse> {
  return getJson<ThroughputResponse>(`/api/metrics/throughput?window=${encodeURIComponent(window)}`);
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

export function startFilterQualityRun(): Promise<FilterQualityStartRunResponse> {
  return postJson<FilterQualityStartRunResponse>("/api/filter-quality/runs");
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
