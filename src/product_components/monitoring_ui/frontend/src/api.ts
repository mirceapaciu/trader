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

const apiBaseUrl = import.meta.env.VITE_UI_API_BASE_URL ?? "";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`);
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
