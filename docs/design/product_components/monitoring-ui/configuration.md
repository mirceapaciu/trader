# Monitoring UI Configuration

Configuration owned by the Monitoring UI process.

## Environment Variables

```bash
# Runtime
UI_HOST=127.0.0.1
UI_PORT=8080
UI_REFRESH_INTERVAL_SECONDS=15
UI_PROVIDER_REFRESH_INTERVAL_SECONDS=10
UI_ALERTS_REFRESH_INTERVAL_SECONDS=20
UI_BACKTEST_REFRESH_INTERVAL_SECONDS=30

# Frontend API access
UI_API_BASE_URL=http://localhost:8080/api

# Query behavior
UI_QUERY_TIMEOUT_SECONDS=5
UI_STALE_DATA_TTL_SECONDS=120
UI_DEFAULT_TIME_WINDOW=1d

# Watchlist admin lookup
MASSIVE_API_KEY=
MASSIVE_API_BASE_URL=https://api.polygon.io
ALPHA_VANTAGE_API_KEY=
INSTRUMENT_LOOKUP_CACHE_TTL_SECONDS=21600
INSTRUMENT_ALIAS_CACHE_TTL_SECONDS=86400
INSTRUMENT_LOOKUP_PROVIDER_DEBOUNCE_MS=300

# Filter Quality panel
FILTER_QUALITY_DB_SCHEMA=filter_quality_evaluator
FILTER_QUALITY_RUN_TIMEOUT_SECONDS=1800

# ThesisBuilder panel alerts
UI_THESIS_BUILDER_STALL_THRESHOLD_SECONDS=600

# Backtest panel
BACKTESTER_DB_SCHEMA=backtester
BACKTESTER_RUN_TIMEOUT_SECONDS=3600

# Shared instrument registry access
SHARED_DB_SCHEMA=shared
WATCHLIST_TABLE=t_watchlist_tickers

```

## Shared Dependencies

Monitoring UI also depends on shared PostgreSQL connection, queue settings, and operational settings defined in `docs/design/shared/configuration.md`.
This includes `QUEUE_URL`, `NEWS_RAW_QUEUE`, and `FAILED_MESSAGES_DLQ` for ThesisBuilder dead-letter telemetry shown in the ThesisBuilder tab.

The ThesisBuilder panel reads ThesisBuilder-owned settings from `docs/design/product_components/thesis-builder/configuration.md` to interpret telemetry. This includes `THESIS_BUILDER_DB_SCHEMA`, `THESIS_BUILDER_CONSUMER_GROUP`, and `THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES`. Monitoring UI does not own or redefine those values; it only uses them when querying ThesisBuilder-owned tables and calculating pending evidence-window status.

At startup, the backend loads `.env.shared`, `.env.prod`, `.env.monitoring-ui`, `.env.news-fetcher`, optional `.env.filter-quality-evaluator`, optional `.env.thesis-builder`, optional `.env.backtester`, and `.env.secrets`. UI-triggered filter quality runs use `FilterQualityEvaluatorSettings.from_env()` and run inside the Monitoring UI backend process. UI-triggered backtest runs likewise use `BacktesterSettings.from_env()` and run as an in-process background run inside the Monitoring UI backend process.

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
- ThesisBuilder variables remain owned by `docs/design/product_components/thesis-builder/configuration.md`.
- Shared cross-process variables belong in `docs/design/shared/configuration.md`.
## Taxonomy decision authorization

```env
UI_TAXONOMY_DECISIONS_ENABLED=false
UI_ADMIN_PASSWORD=replace-with-a-secret-from-.env.secrets
UI_ADMIN_SESSION_TTL_SECONDS=28800
UI_ADMIN_LOGIN_WINDOW_SECONDS=900
UI_ADMIN_LOGIN_MAX_ATTEMPTS=5
UI_ADMIN_ALLOWED_ORIGIN=https://monitoring.example.com
THESIS_BUILDER_TAXONOMY_COMMAND_QUEUE=taxonomy_command_queue
```

Taxonomy mutation is unavailable unless it is explicitly enabled and
`UI_ADMIN_PASSWORD` is supplied from untracked `.env.secrets`. The only account is
`admin`; its server-derived actor is never accepted from a browser payload or header.
The legacy `UI_TAXONOMY_TRUSTED_ACTOR_HEADER` cannot be combined with this mode.
Sessions are in-memory, expire after the configured bounded lifetime, and are lost on
restart. Login and authenticated mutation must be served over HTTPS outside loopback
development; set `UI_ADMIN_ALLOWED_ORIGIN` to the public HTTPS UI origin.
