# Ubuntu GitHub Actions Deployment

This deployment path targets a single Ubuntu server (`haas`) and runs the
Trader services with Docker Compose. GitHub Actions performs the deployment on a
self-hosted runner on the server.

## Runtime Layout

- Compose file: `deploy/docker-compose.yml`
- Deploy entrypoint: `scripts/deployment/prod/deploy.sh`
- GitHub Actions workflow: `.github/workflows/deploy-haas.yml`
- Default public service: Monitoring UI backend plus built React UI on
  `127.0.0.1:8090` unless `TRADER_UI_BIND_IP` is set
- Default worker services: `news-fetcher`, `thesis-builder`
- Opt-in worker service: `trade-executor`, enabled only when
  `TRADER_ENABLE_TRADE_EXECUTOR=true`

The existing `gh-runner` account on `haas` is currently used by the
`mirceapaciu/stock_analyst` runner service. Register a separate runner for
`mirceapaciu/trader` and give it the custom label `trader-prod` so this repo's
workflow can target it without colliding with the other project.

## Server Bootstrap

On `haas`, create the environment directory owned by the runner user:

```bash
sudo install -d -o gh-runner -g gh-runner -m 700 /home/gh-runner/trader-env
```

Create these files in `/home/gh-runner/trader-env`:

```text
.env.postgres
.env.shared
.env.prod
.env.monitoring-ui
.env.news-fetcher
.env.thesis-builder
.env.secrets
```

Optional files:

```text
.env.redis
.env.trade-executor
.env.backtester
```

Use `deploy/env-templates/prod/*.template` as the starting point for production
hosts. Copy them into `/home/gh-runner/trader-env`, remove the `.template`
suffix, and fill every `<REQUIRED_...>` placeholder. The root-level
`.env*.example` files are local-development examples and may use host-local
defaults that are wrong inside Docker.
`.env.postgres` must contain the variables required by the official Postgres
image and the app:

```bash
POSTGRES_DB=trader
POSTGRES_USER=trader
POSTGRES_PASSWORD=<strong password>
```

If Redis auth is desired, set `REDIS_PASSWORD` in `.env.shared` so both the app
containers and the Redis container receive the same value:

```bash
REDIS_PASSWORD=<strong password>
```

`.env.redis` is optional and should only be used when the Redis container needs
an override that differs from the shared app configuration.

The compose file overrides container network locations at deploy time:

```text
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
QUEUE_URL=redis://redis:6379/0
UI_HOST=0.0.0.0
UI_PORT=8090
UI_API_BASE_URL=/api
```

The Haas workflow sets `TRADER_UI_BIND_IP=100.107.130.22`, the server's
Tailscale address, so the UI is reachable from the tailnet at
`http://haas:8090` without binding to every public interface.

The Trader Compose network uses a fixed private subnet, `172.30.50.0/24`, so
IB Gateway trusted IPs can be stable:

```text
172.30.50.10 = monitoring-ui
172.30.50.11 = thesis-builder
172.30.50.12 = trade-executor
```

IB Gateway should trust those addresses when app containers connect to a host
Gateway on `IBKR_HOST=host.docker.internal`. The Gateway should also trust
`127.0.0.1` for host-local smoke tests.

The production deploy script refuses to proceed if the rendered app container
configuration contains `IBKR_HOST=127.0.0.1`, because that would point at the
container itself instead of the Haas host running IB Gateway.

## Manual Deploy

From a checkout of this repo on `haas`:

```bash
export TRADER_ENV_DIR=/home/gh-runner/trader-env
bash scripts/deployment/prod/deploy.sh
```

The script builds the image, starts the default services, prints compose status,
and checks `http://127.0.0.1:8090/api/health`.

To include TradeExecutor:

```bash
export TRADER_ENV_DIR=/home/gh-runner/trader-env
export TRADER_ENABLE_TRADE_EXECUTOR=true
bash scripts/deployment/prod/deploy.sh
```

## GitHub Actions Deploy

Register the `mirceapaciu/trader` self-hosted runner on `haas` with labels:

```text
self-hosted
Linux
trader-prod
```

After the runner is online, pushes to `main` deploy automatically. The workflow
can also be run manually from GitHub Actions with `workflow_dispatch`.

## Operational Commands

```bash
docker compose --project-name trader --file deploy/docker-compose.yml ps
docker compose --project-name trader --file deploy/docker-compose.yml logs --tail=100 monitoring-ui
docker compose --project-name trader --file deploy/docker-compose.yml logs --tail=100 news-fetcher
docker compose --project-name trader --file deploy/docker-compose.yml logs --tail=100 thesis-builder
```

Stop the app services while keeping volumes:

```bash
docker compose --project-name trader --file deploy/docker-compose.yml down
```

Do not remove volumes unless intentionally wiping Postgres and Redis state.
