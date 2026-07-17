# Trader

AI-powered trading bot platform that:

- Fetches financial news
- Infers trading recommendations
- Executes trades through Interactive Brokers (IBKR) integrations
- Provides a UI for monitoring and manual overrides

The project one-pager is here: [Project one-pager](docs/one-pager.md)

## Project Status

Active development.

## Repository Structure

- src: application code
- tests: automated tests
- docs: design, architecture, configuration, and data model specifications
- scripts/deployment: local infrastructure helpers for PostgreSQL and Redis

For authoritative module ownership and boundaries, use the design docs instead of this summary.

## Core Architecture

Architecture boundaries and detailed ownership are maintained in

- [docs/design/overview](./docs/design/overview.md)

## Prerequisites

- Python 3.14
- uv package manager
- Docker (if you want to use the deployment scripts for PostgreSQL and Redis)

## Local Development

1. Start PostgreSQL:
   - PowerShell: scripts/deployment/postgres/start.ps1
   - Bash: scripts/deployment/postgres/start.sh
2. Start Redis:
   - PowerShell: scripts/deployment/redis/start.ps1
   - Bash: scripts/deployment/redis/start.sh
3. Install dependencies with your preferred Python workflow (uv recommended).
4. Run tests.
5. Start news-fetcher:
   - PowerShell: scripts/deployment/news-fetcher/start.ps1
   - Bash: scripts/deployment/news-fetcher/start.sh
   - Module entrypoint: uv run python -m src.product_components.news_fetcher
6. Start monitoring UI:
   - PowerShell: scripts/deployment/monitoring-ui/start.ps1
   - Bash: scripts/deployment/monitoring-ui/start.sh
   - Config: `.env.monitoring-ui` (see `.env.monitoring-ui.example`)
   - Stop PowerShell: scripts/deployment/monitoring-ui/stop.ps1
   - Stop Bash: scripts/deployment/monitoring-ui/stop.sh
   - Default URLs: backend `http://127.0.0.1:8090`, frontend `http://127.0.0.1:5174`
   - Logs: `logs/monitoring-ui-backend.log`, `logs/monitoring-ui-frontend.log`
   - PID files: `logs/monitoring-ui-backend.pid`, `logs/monitoring-ui-frontend.pid`
   - Optional PowerShell ports: `scripts/deployment/monitoring-ui/start.ps1 -BackendPort 8091 -FrontendPort 5175`
   - Optional Bash ports: `scripts/deployment/monitoring-ui/start.sh 8091 5175`
   - The Filter Quality panel starts an in-process evaluator run for the last 24 hours. Accepted-audit sampling is off by default; results are persisted in `filter_quality_evaluator.t_filter_quality_runs`.

News-fetcher source toggles are configured in `.env.news-fetcher` (see `.env.news-fetcher.example`):
- `NEWS_SOURCE_FINNHUB_ENABLED=true|false`
- `NEWS_SOURCE_MARKETAUX_ENABLED=true|false`
- `NEWS_SOURCE_RSS_ENABLED=true|false`

The news-fetcher startup path applies the shared and news-fetcher schema SQL files on startup, so local runs can create missing tables such as `shared.t_watchlist_tickers` automatically.

## Test Commands

- Unit tests: uv run pytest -m "not integration"
- Integration tests: uv run pytest -m integration
- Full suite: uv run pytest
- Monitoring UI frontend build: cd src/product_components/monitoring_ui/frontend; npm run build

## Ubuntu Deployment

Production deployment for the `haas` Ubuntu server is Docker Compose based:

- Compose stack: `deploy/docker-compose.yml`
- Deploy script: `scripts/deployment/prod/deploy.sh`
- GitHub Actions workflow: `.github/workflows/deploy-haas.yml`
- Server bootstrap notes: [Ubuntu GitHub Actions Deployment](docs/design/deployment/ubuntu-github-actions.md)

The workflow expects a self-hosted runner registered to this repository with
the `trader-prod` label and production env files under
`/home/gh-runner/trader-env`.

## Environment Separation

Use separate database configuration for test and production-like runs.

- Shared defaults: `.env.shared`
- Monitoring UI overrides: `.env.monitoring-ui` (or `.env.monitoring-ui.example` as template)
- Test overrides: `.env.test` (or `.env.test.example` as template)
- Production overrides: `.env.prod` (or `.env.prod.example` as template)

Integration tests enforce safety checks and must run against a test DB that differs from production DB.

PowerShell example for integration tests:

```powershell
Get-Content .env.shared | ForEach-Object {
   if ($_ -match '^[A-Za-z_][A-Za-z0-9_]*=') {
      $name, $value = $_ -split '=', 2
      Set-Item -Path "Env:$name" -Value $value
   }
}
Get-Content .env.test | ForEach-Object {
   if ($_ -match '^[A-Za-z_][A-Za-z0-9_]*=') {
      $name, $value = $_ -split '=', 2
      Set-Item -Path "Env:$name" -Value $value
   }
}
uv run pytest -m integration -q
```

Minimum required DB separation:

- `POSTGRES_DATABASE=trader_test`
- `POSTGRES_PROD_DATABASE=trader`

## Licensing

This project uses a dual-license model:

- Open source license: GNU Affero General Public License v3.0 (AGPL-3.0-only)
- Commercial licensing: available via separate agreement

See LICENSE and COMMERCIAL-LICENSE.md for details.

## Disclaimer

Use of this software is at your own risk. The authors and contributors are not
liable for financial loss or any other damages resulting from use of this
project.

See DISCLAIMER.md for the full legal and financial disclaimer.

## Commercial Use

If your intended use is not compliant with AGPL obligations, contact the project owner for a commercial license agreement.
