# Monitoring UI Configuration

Configuration owned by the Monitoring UI process.

## Environment Variables

```bash
# Runtime
UI_PORT=8080
UI_REFRESH_INTERVAL_SECONDS=15
UI_PROVIDER_REFRESH_INTERVAL_SECONDS=10
UI_ALERTS_REFRESH_INTERVAL_SECONDS=20

# Query behavior
UI_QUERY_TIMEOUT_SECONDS=5
UI_STALE_DATA_TTL_SECONDS=120
UI_DEFAULT_TIME_WINDOW=1h

```

## Shared Dependencies

Monitoring UI also depends on shared PostgreSQL connection, queue settings, and operational settings defined in `docs/design/shared/configuration.md`.

## Ownership Rules

- Variables prefixed with `UI_` are owned by Monitoring UI.
- NewsFetcher variables remain owned by `docs/design/product-components/news-fetcher/configuration.md`.
- Shared cross-process variables belong in `docs/design/shared/configuration.md`.