# Monitoring UI Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the Monitoring UI process.

Monitoring UI responsibilities:
- Present near-real-time NewsFetcher operational state to the operator.
- Surface provider health, throughput, deduplication, and publish outcomes.
- Surface queue and dependency health relevant to NewsFetcher.
- Start a bounded Filter Quality Evaluator run and surface the latest persisted evaluator summary.

Out of scope:
- Fetching or mutating provider payloads.
- News normalization, filtering, deduplication, and publishing logic.
- Trade decision logic and trade execution.

## 2. Process Contract

Process name:
- monitoring_ui

Inputs:
- NewsFetcher operational telemetry (metrics and health state).
- NewsFetcher process logs and cycle summaries.
- Read-only projections from NewsFetcher-owned persistence tables.
- Environment configuration.

Outputs:
- Operator-visible dashboards and detail views.
- Optional operator action audit records.
- Optional non-blocking alerts (for example, webhook or email) when configured.

Delivery semantics:
- Read-mostly, eventually consistent UI state.
- UI must not block NewsFetcher runtime progress.

## 3. Runtime and Refresh Windows

Default refresh rules:
- Overview dashboard refreshes every `UI_REFRESH_INTERVAL_SECONDS`.
- Provider detail view refreshes every `UI_PROVIDER_REFRESH_INTERVAL_SECONDS`.
- Error and dead-letter views refresh every `UI_ALERTS_REFRESH_INTERVAL_SECONDS`.

Runtime behavior:
- If automatic refresh is enabled, polling runs continuously.
- If automatic refresh is disabled, only manual refresh updates state.
- UI must display last successful refresh timestamp in UTC.

## 4. Dashboard Behavior

### 4.1 Global Health Panel

Must display:
- Readiness state (`healthy` or `unhealthy`).
- Liveness state (`healthy` or `unhealthy`).
- Last successful cycle timestamp per enabled provider.
- Active incident count.

Health aggregation rules:
- Overall readiness is unhealthy if NewsFetcher database or queue dependency is unhealthy.
- Overall liveness is unhealthy if no enabled provider has a successful cycle within two poll intervals.

### 4.2 Provider Status Panel

For each enabled provider (`finnhub`, `marketaux`, `rss:<feed-name>`), display:
- Last cycle start and end timestamps.
- Last cycle duration.
- Last cycle fetch count and fetch error count.
- Last cycle dedupe drop counts (strong and soft).
- Last cycle persist success and publish success counts.
- Last error class and timestamp when present.

### 4.3 Throughput and Quality Panels

Must display time-series and aggregates for:
- `fetch_count` by provider.
- `fetch_error_count` by provider and error class.
- `dedupe_drop_count` by duplicate type.
- `persist_success_count` and `persist_error_count`.
- `publish_success_count` and `publish_error_count`.
- `end_to_end_latency_seconds` distribution.

Time windows:
- 15 minutes, 1 hour, 24 hours, and custom bounded range.

### 4.3.1 Filter Quality Panel

The dashboard includes a compact Filter Quality panel.

The panel must display:
- Whether a filter quality run is currently `running`.
- The latest terminal run status: `completed`, `failed`, or no prior runs.
- The latest terminal run metrics: rejection precision proxy, incorrectly accepted rate estimate, rejected evaluated count, accepted sampled count, dataset input count, finished timestamp, and failed error code when present.

The Run filter quality button starts one evaluator run for the last 24 hours. UI-triggered runs use the active NewsFetcher filter configuration, set `accepted_audit_enabled=false`, and write their status and summary to `filter_quality_evaluator.t_filter_quality_runs`. If a run is already active, the API returns `409` with the active `run_id`.

### 4.4 Backlog and Dead-Letter Panels

Must display:
- Count of pending publish obligations.
- Count of retrying obligations and max attempt age.
- Count of dead-lettered envelopes.
- Recent dead-letter items with source, reason, and first failure time.

## 5. Implementation Stack

The Monitoring UI frontend stack is:
- React for dashboard composition and interactive controls.
- TypeScript for typed API contracts and state models.
- Vite for frontend build tooling and local development.
- TanStack Query for server-state caching, polling, retries, and stale-data handling.
- React Router for dashboard navigation.
- Recharts for standard dashboard charts.
- uPlot for dense time-series charts when Recharts cannot meet performance needs.
- Radix primitives or a thin component layer such as shadcn/ui for accessible tabs, dialogs, menus, and controls.

The Monitoring UI backend adapter stack is:
- FastAPI for HTTP endpoints consumed by the frontend.
- Pydantic models for request and response contracts.

The UI must not connect directly to PostgreSQL or Redis from browser code. Browser code reads and mutates state only through the Monitoring UI HTTP API.

## 6. Source Organization

Default implementation placement:
- Frontend source: `src/product_components/monitoring_ui/frontend`.
- Backend adapter source: `src/product_components/monitoring_ui/backend`.

The Monitoring UI is a product-owned component. Both browser code and its backend API adapter live under the same product component boundary.

## 7. HTTP API Contract

The Monitoring UI HTTP API is a thin read-mostly adapter over component telemetry, persistence projections, and queue health. It must not embed NewsFetcher normalization, deduplication, publishing, analyzer, or trade execution logic.

Required read endpoints:
- `GET /api/health` returns global readiness, liveness, dependency state, and stale-data status.
- `GET /api/providers` returns provider-level cycle summaries and last error state.
- `GET /api/metrics/throughput` returns bounded-window throughput and quality metrics.
- `GET /api/backlog` returns pending, retrying, and dead-letter counts.
- `GET /api/dead-letter` returns recent dead-letter items with bounded pagination.
- `GET /api/filter-quality` returns the current running evaluator run, latest terminal run, and generated timestamp.

Optional operator-action endpoints:
- `POST /api/actions/refresh` triggers a non-blocking refresh of UI projections when supported.
- `POST /api/actions/alert-test` sends a test alert when alert webhooks are enabled.
- `POST /api/filter-quality/runs` starts an in-process background Filter Quality Evaluator run for the last 24 hours and returns `202` when accepted.

All response timestamps must be UTC ISO 8601 strings. Endpoint responses must include enough metadata for the frontend to render degraded panels without treating one failed source as a full dashboard failure.

## 8. Live Update Policy

The first implementation uses HTTP polling through TanStack Query. Poll intervals follow the `UI_*_REFRESH_INTERVAL_SECONDS` settings.

Server-sent events or WebSockets may be added later only for workflows that need lower-latency updates, such as live order status, incident notifications, or queue-drain progress. Polling remains the baseline because the Monitoring UI state is read-mostly and eventually consistent.

## 9. Configuration Requirements

Required variables:
- UI_PORT
- UI_API_BASE_URL
- UI_REFRESH_INTERVAL_SECONDS
- UI_PROVIDER_REFRESH_INTERVAL_SECONDS
- UI_ALERTS_REFRESH_INTERVAL_SECONDS
- UI_QUERY_TIMEOUT_SECONDS
- UI_STALE_DATA_TTL_SECONDS

Optional variables:
- UI_DEFAULT_TIME_WINDOW
- UI_EXPORT_MAX_ROWS
- UI_ENABLE_ALERT_WEBHOOK
- UI_ALERT_WEBHOOK_URL

## 10. Acceptance Criteria

Implementation is acceptable when all are true:
- UI renders global health and provider status for all enabled providers.
- Throughput and error metrics are visible for required windows.
- Degraded data-source conditions are explicit and non-blocking.
- UI never mutates NewsFetcher article or checkpoint state.
- Browser code communicates through the Monitoring UI HTTP API and does not connect directly to PostgreSQL or Redis.
- Required tests pass.

## 11. Minimum Test Plan

Unit tests:
- Health aggregation rules from provider-level inputs.
- Panel degradation behavior on source-specific failures.
- Stale-data banner behavior and TTL enforcement.

Integration tests:
- Dashboard loads with NewsFetcher telemetry backend.
- Partial data source outage renders degraded panels without full failure.
- Dead-letter panel correctly renders retry exhaustion cases.
