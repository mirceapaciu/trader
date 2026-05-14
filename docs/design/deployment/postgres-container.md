# PostgreSQL Container Deployment Specification

## Purpose

Define a repeatable, project-owned way to run PostgreSQL locally using Docker Compose.

## Goals

- Keep database setup versioned in the repository
- Ensure consistent environments across machines
- Support the project data model with per-component schemas

## Scope

This specification covers local development deployment of PostgreSQL as a container.
It does not cover production managed PostgreSQL operations.

## Deployment Assets

- Compose file: `scripts/deployment/postgres/docker-compose.yml`
- Environment template: `scripts/deployment/postgres/.env.example`
- Schema init script: `scripts/deployment/postgres/init/01-create-schemas.sql`
- Start helper (PowerShell): `scripts/deployment/postgres/start.ps1`
- Stop helper (PowerShell): `scripts/deployment/postgres/stop.ps1`
- Start helper (Bash): `scripts/deployment/postgres/start.sh`
- Stop helper (Bash): `scripts/deployment/postgres/stop.sh`

## Runtime Configuration

Environment variables required by the compose service:

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_PORT`

Application settings should match the design configuration:

- `DB_BACKEND=postgres`
- `POSTGRES_HOST=127.0.0.1`
- `POSTGRES_PORT=5432` (or overridden value)
- `POSTGRES_DATABASE=trader`
- `POSTGRES_USER=trader`
- `POSTGRES_SSLMODE=disable` for local

## Container Specification

- Image: `postgres:16-alpine`
- Restart policy: `unless-stopped`
- Health check using `pg_isready`
- Persistent volume: `postgres_data`
- Init script mounted from `init/` to create component schemas

## Database Schemas

The init script creates these schemas:

- `news_fetcher`
- `analyzer_worker`
- `trade_executor`
- `shared`

## Deployment Procedure

1. Copy `.env.example` to `.env` in `scripts/deployment/postgres`.
2. Set `POSTGRES_PASSWORD` in `.env`.
3. Start PostgreSQL:
   - `pwsh -File scripts/deployment/postgres/start.ps1`
   - `bash scripts/deployment/postgres/start.sh`
4. Verify health:
   - `docker compose -f scripts/deployment/postgres/docker-compose.yml ps`
5. Validate DB access:
   - `docker exec -it trader-postgres psql -U trader -d trader -c "\dn"`

## Shutdown Procedure

- Stop service:
  - `pwsh -File scripts/deployment/postgres/stop.ps1`
  - `bash scripts/deployment/postgres/stop.sh`

## Data Persistence

- Data is persisted in Docker volume `postgres_data`.
- To reset local state completely:
  - `docker compose -f scripts/deployment/postgres/docker-compose.yml down -v`

## Notes

- Keep secrets out of committed files.
- Use `.env` locally and secret management for non-local environments.
