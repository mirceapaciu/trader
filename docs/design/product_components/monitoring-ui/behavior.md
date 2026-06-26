# Monitoring UI Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the Monitoring UI process.

Monitoring UI responsibilities:
- Present near-real-time NewsFetcher operational state to the operator.
- Present ThesisBuilder operational KPIs in a separate domain tab.
- Surface provider health, throughput, deduplication, and publish outcomes.
- Surface queue and dependency health relevant to NewsFetcher.
- Start a bounded Filter Quality Evaluator run and surface the latest persisted evaluator summary.
- Start a bounded Backtester run and surface backtest run summaries, equity curves, per-strategy
  metrics, pipeline-delay breakdowns, and per-trade results in a dedicated Backtest tab.

Out of scope:
- Fetching or mutating provider payloads.
- News normalization, filtering, deduplication, and publishing logic.
- Trade decision logic and trade execution.

## 2. Process Contract

Process name:
- monitoring_ui

Inputs:
- NewsFetcher operational telemetry (metrics and health state).
- ThesisBuilder audit tables and evidence-window state.
- Backtester run summaries, per-trade results, and equity curves (read-only projections).
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

The Monitoring UI is organized as a modular tabbed application. The default tab is `NewsFetcher`; ThesisBuilder monitoring appears in a separate `ThesisBuilder` tab. Domain-specific data access, frontend state, and dashboard composition should stay separated in source code.

The Monitoring UI also includes a top-level `Watchlist` tab for operator/admin workflows around shared watchlist membership and instrument alias maintenance. This tab is distinct from NewsFetcher filter tuning and must use the Monitoring UI HTTP API rather than direct browser access to external providers or databases.

The Monitoring UI also includes a top-level `Backtest` tab for triggering and reviewing historical backtest runs produced by the Backtester component. Like the other tabs, it accesses data only through the Monitoring UI HTTP API and keeps its data access, frontend state, and composition separated in source code.

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
- Last non-zero fetch timestamp and fetch error count.
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
- 15 minutes, 1 hour, 1 day, 7 days, and 30 days.

Throughput window controls:
- The Throughput panel defaults to the `1d` preset on first load.
- Operators can switch between `15m`, `1h`, `1d`, `7d`, and `30d` presets without leaving the dashboard.
- Operators can optionally set a single window-end date to anchor the selected preset in the past.
- When a window-end date is set, the frontend derives explicit `start_at` and `end_at` bounds from the selected preset duration, sends the selected preset window alongside those bounds, and the backend keeps that preset's granularity for bucketing.
- The UI presents the preset duration and optional window-end date as one control group so it is clear they work together rather than as competing range modes.
- The API rejects unsupported window tokens, missing custom bounds, inverted ranges, and custom ranges longer than the configured safe bound of 30 days.
- Throughput buckets are derived from obligation `created_at` for all displayed counts because the current outbox model stores only the latest status, not an append-only status transition history.
- Default throughput bucket granularity follows the selected window:
  - `15m` and `1h`: raw `created_at` timestamps
  - `1d` and `7d`: `date_trunc('hour', created_at)`
  - `30d`: `date_trunc('day', created_at)`

### 4.3.1 Filter Quality Panel

The dashboard includes a compact Filter Quality panel.

The panel must display:
- Whether a filter quality run is currently `running`.
- The latest terminal run status: `completed`, `failed`, or no prior runs.
- The latest terminal run metrics: incorrectly rejected rate, incorrectly accepted rate estimate, rejected evaluated count, accepted audited count, dataset input count, finished timestamp, and failed error code when present.

The metrics are arranged in this order for consistent scanning:
- `Overview`: total quality, dataset input, last finished.
- `Rejected`: incorrectly rejected rate, rejected evaluated, incorrectly rejected count.
- `Accepted`: incorrectly accepted rate, accepted audited, incorrectly accepted count.
- `Errors`: item failures and failure reason.

The Evaluate production filter control starts one evaluator run for the last 24 hours. UI-triggered runs use the active NewsFetcher filter configuration and write their status and summary to `filter_quality_evaluator.t_filter_quality_runs`. Accepted-article evaluation is off by default in the UI. A checkbox labeled `Evaluate accepted articles` lets the operator opt into accepted-news auditing for a specific run; when enabled, the run sets `accepted_audit_enabled=true` and uses `FILTER_QUALITY_ACCEPTED_AUDIT_SAMPLE_SIZE` as the accepted-news audit sample size. If a run is already active, the API returns `409` with the active `run_id`.

The UI seeds the displayed test-filter draft from the latest run's evaluated filter snapshot when available. For production evaluations, incorrectly rejected details display the production rejection reason; for simulation evaluations, they display the simulation rejection reason. Deterministic rejection reasons such as duplicates and excluded keywords take precedence over persisted LLM cause and solution text when displaying the cause and solution columns. The displayed draft is not persisted until the operator saves the test filter.

Incorrectly rejected records use a review-oriented split view. The list remains compact for quick browsing and shows headline, recommended keyword chips, source, published time, rejection reason, cause, and suggested solution. Selecting a record opens a detail panel that keeps the list visible while showing the full stored article summary, external article link, matched duplicate article when available, classifier rationale, suggested action, and item-specific recommended keyword chips. Full summaries are not rendered as a table column because variable-length article text would make the list difficult to scan.

The detail panel allows operators to select article text and convert that selection into a manual keyword chip. Manual chips are visually distinct from evaluator-recommended chips, are removable before saving, and are persisted only when the operator clicks `Add to test filter`. They must not be presented as evaluator recommendations because they are operator-authored filter edits. The UI labels this section as `Manual keywords` to avoid confusion with selected evaluator recommendations.

Incorrectly accepted records use a similar split view. The count tile is clickable and opens a compact list of accepted articles that were classified as low-value noise, with headline, published time, source, probable cause, confidence, and suggested action. Selecting a record opens a detail panel with the stored summary, external article link, classifier rationale, and suggested action. This accepted-news review flow is read-only in the UI and does not reuse the incorrectly rejected keyword-edit workflow.

### 4.4 Backlog and Dead-Letter Panels

Must display:
- Count of pending publish obligations.
- Count of retrying obligations and max attempt age.
- Count of dead-lettered envelopes.
- Recent dead-letter items with source, reason, and first failure time.

### 4.5 ThesisBuilder Panel

The ThesisBuilder tab displays KPI tiles for:
- Number of articles processed.
- Number of market-moving articles.
- Number of articles included into thesis cards.
- Number of articles too old to be included into valid cards.
- Number of created thesis cards.
- Number of currently pending thesis cards.
- Number of ThesisBuilder dead-lettered consumer failures.
- Oldest and average pending thesis-card age.
- Minimum and average time remaining before pending evidence windows expire.
- Estimated missed thesis cards caused by stale evidence.
- Average, p95, and max evidence-age exceedance for stale audit cards.

Time windows:
- 15 minutes, 1 hour, 1 day, 7 days, and 30 days.

Pending thesis-card rows show ticker, exchange, strategy, direction, pending age, and time to expiry for collecting evidence windows. Stale audit cards are cards with `validation_status=rejected` and `rejection_reason_code=stale_evidence`; the operator-facing label is `stale`.

The ThesisBuilder tab also shows a recent dead-letter panel sourced from `failed_messages_dlq` entries written by ThesisBuilder when message consumption fails after validation or payload checks. These items are separate from the NewsFetcher outbox dead-letter views:
- NewsFetcher dead letters represent publication-obligation failures from `news_fetcher.t_publication_obligations`.
- ThesisBuilder dead letters represent downstream consumer failures while processing `news_raw_queue` messages.

If ThesisBuilder tables or required columns are not available, the tab renders a degraded empty state without failing the NewsFetcher monitoring tab.

### 4.6 Backtest Tab

The Backtest tab lets the operator trigger backtest runs and review their results. It reads only
through the Monitoring UI HTTP API, which projects read-only data from the `backtester` schema; it
must not embed simulation logic, which is owned by the Backtester component.

#### Trigger control

A `Run backtest` control starts one bounded Backtester run with operator-selected parameters:
- `window_start_at` and `window_end_at` (UTC),
- `mode` (`replay` default, or `regeneration`),
- `timing_scenario` (`ideal` default, `actual`, or `both`),
- `card_population` (`all` default, `approved_only`, or `rejected_only`),
- optional `strategies` subset and `initial_capital`.

Trigger behavior mirrors the Filter Quality control:
- The run executes as an in-process background run inside the Monitoring UI backend and writes its
  status and results to the `backtester` schema.
- If a backtest run is already active, the API returns `409` with the active `run_id`.
- The UI-triggered path is intended for bounded `replay` runs. Long or expensive runs, especially
  `regeneration` mode, should be started from the CLI; the UI surfaces a warning when `regeneration`
  is selected.

#### Run list

A list of recent runs shows, per run: `run_id`, status (`running`, `completed`, `failed`), window,
`mode`, `timing_scenario`, `card_population`, and headline metrics (net P&L, total return, win rate,
profit factor, max drawdown), with created and finished timestamps. Selecting a run opens its detail.

#### Run detail

The detail view keeps the run list visible and shows:
- Summary tiles: total return, net P&L, win rate, profit factor, expectancy, max drawdown, Sharpe
  ratio, number of trades, exposure fraction, and signal accuracy.
- Equity curve chart. For a `both` run, the ideal and actual equity curves are overlaid.
- Per-strategy breakdown table of the same trade-level metrics for each strategy in the run.
- Card-status breakdown: metrics restricted to `approved`, `rejected`, and `stale_evidence` cards,
  plus the live-executable slice (`card_was_live_expired = false`), so the live-fidelity subset can be
  compared with the full thesis population.
- Pipeline-delay panel: avg/p95/max of `news_fetch_delay`, `thesis_build_delay`, and
  `total_pipeline_delay`, with a histogram grouped by the configured delay buckets.
- Ideal-vs-actual gap tiles: `pnl_gap`, `win_rate_gap`, and `trades_flipped_by_delay`. Shown only when
  `timing_scenario = both`; otherwise the panel is hidden with an explanatory note.
- Per-trade table with bounded pagination: ticker, exchange, strategy, direction,
  `entry_timing_scenario`, entry/exit time and price, net P&L, return, `exit_reason`, the three delay
  values, and card status. Filterable by scenario, strategy, exit reason, and card status.

Time windows for the run list follow the standard `15m`, `1h`, `1d`, `7d`, and `30d` presets over run
`created_at`.

If `backtester` tables or required columns are not available, the tab renders a degraded empty state
without failing other tabs.

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
- `GET /api/metrics/throughput` returns bounded-window throughput and quality metrics for either a preset `window` token or explicit `start_at` and `end_at` bounds.
- `GET /api/thesis-builder/metrics` returns bounded-window ThesisBuilder KPIs and pending evidence windows for `15m`, `1h`, `1d`, `7d`, or `30d`.
- `GET /api/backlog` returns pending, retrying, and dead-letter counts.
- `GET /api/dead-letter` returns recent dead-letter items with bounded pagination.
- `GET /api/filter-quality` returns the current running evaluator run, latest terminal run, and generated timestamp.
- `GET /api/filter-quality/runs/{run_id}/incorrectly-rejected` returns review-oriented incorrectly rejected article details for one run.
- `GET /api/filter-quality/runs/{run_id}/incorrectly-accepted` returns review-oriented incorrectly accepted article details for one run.
- `GET /api/backtests` returns recent backtest runs with status, parameters, and headline metrics for a `15m`, `1h`, `1d`, `7d`, or `30d` window, plus the currently running run when present.
- `GET /api/backtests/{run_id}` returns one run's full summary: scalar metrics, per-strategy breakdown, card-status breakdown, delay aggregates, and ideal-vs-actual gap metrics.
- `GET /api/backtests/{run_id}/trades` returns per-trade rows with bounded pagination and optional filters on timing scenario, strategy, exit reason, and card status.
- `GET /api/backtests/{run_id}/equity` returns equity-curve points for the run, separated by `timing_scenario`.

Optional operator-action endpoints:
- `POST /api/actions/refresh` triggers a non-blocking refresh of UI projections when supported.
- `POST /api/actions/alert-test` sends a test alert when alert webhooks are enabled.
- `POST /api/filter-quality/runs` starts an in-process background Filter Quality Evaluator run for the last 24 hours and returns `202` when accepted.
- `POST /api/backtests` starts an in-process background Backtester run with the operator-selected parameters and returns `202` when accepted, or `409` with the active `run_id` when a run is already in progress.
- `GET /api/watchlist` returns active shared watchlist rows with alias completeness state.
- `GET /api/watchlist/lookups` returns external instrument suggestions for operator search input.
- `POST /api/watchlist` adds or reactivates a shared watchlist entry through the shared instrument-registry/admin capability.
- `PUT /api/watchlist/{ticker}/{exchange_code}` updates persisted display name and aliases for one watchlist row.
- `POST /api/watchlist/{ticker}/{exchange_code}/alias-discovery` attempts shared-owned alias discovery for one watchlist row.
- `DELETE /api/watchlist/{ticker}/{exchange_code}` soft-disables one watchlist row.

All response timestamps must be UTC ISO 8601 strings. Endpoint responses must include enough metadata for the frontend to render degraded panels without treating one failed source as a full dashboard failure.

Watchlist lookup ordering rules:
- `GET /api/watchlist/lookups` must return suggestions ordered by descending match quality.
- Exact ticker matches outrank exact alias matches; exact alias matches outrank exact display-name matches.
- Exact-word company-name or alias matches outrank prefix-only and broad substring matches.
- Backend/shared lookup owns suggestion ranking before caching and response serialization. The browser must render returned order and must not apply its own relevance sorting.

### 7.1 Failure Handling Contract

CORS middleware is necessary for local browser development but is not sufficient to make endpoint failures safe. Unhandled backend exceptions that produce `500` responses are a Monitoring UI contract violation because browsers can surface them as misleading cross-origin failures when the response is missing expected application-level handling.

Failure-handling rules:
- Route handlers and service entrypoints must catch infrastructure failures before they escape FastAPI. Relevant failure families include PostgreSQL driver errors, Redis client/connection errors, timeout/network failures from shared provider adapters, and expected degraded-startup repository bootstrap failures.
- `GET /api/health` is always best-effort. It must degrade rather than fail when individual dependencies are unavailable.
- Other read/dashboard endpoints must either:
  - return a degraded `200` response with explicit availability metadata such as `available=false`, `message`, safe empty collections, and `generated_at` when the response contract can represent degraded state without being misleading, or
  - return a structured `503 Service Unavailable` response with a short stable `detail` message when the existing response contract cannot represent degradation cleanly.
- Detail/read endpoints must prefer structured `503` over fabricated empty success when empty data would incorrectly imply that no records exist.
- Mutation/admin endpoints must never degrade to success. They return structured `422` for validation errors, `409` for business conflicts, and `503` for dependency/storage/provider outages.
- API responses must not expose raw driver or provider exception text to the frontend.

Current default guidance:
- `GET /api/health` must degrade.
- Existing dashboard endpoints such as `providers`, `throughput`, `backlog`, `dead-letter`, `filter-quality`, `backtests`, and ThesisBuilder metrics should explicitly choose between degraded `200` and structured `503` based on whether their response models can represent unavailable state safely.
- The `POST /api/backtests` mutation endpoint must never degrade to success: it returns `422` for invalid run parameters, `409` when a run is already active, and `503` on dependency/storage outages.
- `ThesisBuilderMetricsResponse` already supports degraded success with `available` and `message`. Other response models should add comparable fields before adopting degraded `200` behavior.
- New endpoints must reuse a shared backend exception-mapping helper rather than introducing ad hoc route-specific `try/except` logic.

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
- UI_BACKTEST_REFRESH_INTERVAL_SECONDS

## 10. Acceptance Criteria

Implementation is acceptable when all are true:
- UI renders global health and provider status for all enabled providers.
- Throughput and error metrics are visible for required windows.
- Degraded data-source conditions are explicit and non-blocking.
- Backend-managed dependency failures do not surface to the browser as raw `500` errors or misleading pseudo-CORS failures.
- UI never mutates NewsFetcher article or checkpoint state.
- Browser code communicates through the Monitoring UI HTTP API and does not connect directly to PostgreSQL or Redis.
- Required tests pass.

## 11. Minimum Test Plan

Unit tests:
- Health aggregation rules from provider-level inputs.
- Panel degradation behavior on source-specific failures.
- Stale-data banner behavior and TTL enforcement.
- Each new Monitoring UI endpoint must include at least one dependency-unavailable test that verifies degraded `200` behavior or structured `503` handling.

Integration tests:
- Dashboard loads with NewsFetcher telemetry backend.
- Partial data source outage renders degraded panels without full failure.
- Dead-letter panel correctly renders retry exhaustion cases.
