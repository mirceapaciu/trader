# Monitoring UI Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the Monitoring UI process.

Monitoring UI responsibilities:
- Present near-real-time NewsFetcher operational state to the operator.
- Surface provider health, throughput, deduplication, and publish outcomes.
- Surface queue and dependency health relevant to NewsFetcher.

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

### 4.4 Backlog and Dead-Letter Panels

Must display:
- Count of pending publish obligations.
- Count of retrying obligations and max attempt age.
- Count of dead-lettered envelopes.
- Recent dead-letter items with source, reason, and first failure time.

## 9. Configuration Requirements

Required variables:
- UI_PORT
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