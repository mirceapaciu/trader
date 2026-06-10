# Monitoring UI Configuration

Configuration owned by the Monitoring UI process.

## Environment Variables

```bash
# Runtime
UI_PORT=8080
UI_REFRESH_INTERVAL_SECONDS=15
UI_PROVIDER_REFRESH_INTERVAL_SECONDS=10
UI_ALERTS_REFRESH_INTERVAL_SECONDS=20

# Frontend API access
UI_API_BASE_URL=http://localhost:8080/api

# Query behavior
UI_QUERY_TIMEOUT_SECONDS=5
UI_STALE_DATA_TTL_SECONDS=120
UI_DEFAULT_TIME_WINDOW=1h

# Filter Quality panel
FILTER_QUALITY_DB_SCHEMA=filter_quality_evaluator
FILTER_QUALITY_RUN_TIMEOUT_SECONDS=1800

```

## Shared Dependencies

Monitoring UI also depends on shared PostgreSQL connection, queue settings, and operational settings defined in `docs/design/shared/configuration.md`.

At startup, the backend loads `.env.shared`, `.env.prod`, `.env.monitoring-ui`, `.env.news-fetcher`, optional `.env.filter-quality-evaluator`, and `.env.secrets`. UI-triggered filter quality runs use `FilterQualityEvaluatorSettings.from_env()` and run inside the Monitoring UI backend process.

## Stack Decisions

- Frontend runtime: React with TypeScript.
- Frontend build tooling: Vite.
- Server-state and refresh behavior: TanStack Query using HTTP polling by default.
- Navigation: React Router.
- Charting: Recharts by default; uPlot for dense time-series charts when needed.
- Backend adapter: FastAPI with Pydantic request and response models.

The browser UI must use `UI_API_BASE_URL` to reach the Monitoring UI API. It must not connect directly to PostgreSQL or Redis.

## Ownership Rules

- Variables prefixed with `UI_` are owned by Monitoring UI.
- NewsFetcher variables remain owned by `docs/design/product_components/news-fetcher/configuration.md`.
- Shared cross-process variables belong in `docs/design/shared/configuration.md`.
