# Redis Queue Container Deployment Specification

## Purpose

Define a repeatable, project-owned way to run the Redis message broker locally using Docker Compose.

## Goals

- Keep queue infrastructure setup versioned in the repository
- Ensure consistent broker behavior across machines
- Support Redis Streams with consumer groups for multi-consumer processing

## Scope

This specification covers local development deployment of Redis as a standalone queue component.
It does not cover managed Redis services or production HA topology.

## Deployment Assets

- Compose file: `scripts/deployment/redis/docker-compose.yml`
- Environment template: `scripts/deployment/redis/.env.example`
- Redis config: `scripts/deployment/redis/redis.conf`
- Start helper (PowerShell): `scripts/deployment/redis/start.ps1`
- Stop helper (PowerShell): `scripts/deployment/redis/stop.ps1`
- Start helper (Bash): `scripts/deployment/redis/start.sh`
- Stop helper (Bash): `scripts/deployment/redis/stop.sh`

## Runtime Configuration

Environment variables required by the compose service:

- `REDIS_PORT`
- `REDIS_PASSWORD` (optional for local)

Application settings should match the design configuration:

- `QUEUE_BACKEND=redis_streams`
- `REDIS_HOST=127.0.0.1`
- `REDIS_PORT=6379` (or overridden value)
- `REDIS_DB=0`
- `REDIS_STREAM_NEWS_RAW=news_raw_queue`
- `REDIS_STREAM_SIGNAL=signal_queue`
- `REDIS_STREAM_DLQ=failed_messages_dlq`

## Container Specification

- Image: `redis:7-alpine`
- Restart policy: `unless-stopped`
- Health check using `redis-cli ping`
- Persistent volume: `redis_data`
- Config file mounted with append-only persistence enabled

## Stream and Consumer Group Model

Required streams:

- `news_raw_queue`
- `signal_queue`
- `failed_messages_dlq`

Required consumer groups:

- Stream `news_raw_queue`:
  - `analyzer_worker_group`
  - `narrative_aggregator_group` (planned)
- Stream `signal_queue`:
  - `trade_executor_group`

This model ensures each group receives every relevant stream message independently.

## Deployment Procedure

1. Copy `.env.example` to `.env` in `scripts/deployment/redis`.
2. Set `REDIS_PASSWORD` in `.env` if authentication is enabled.
3. Start Redis:
   - `pwsh -File scripts/deployment/redis/start.ps1`
  - `bash scripts/deployment/redis/start.sh`
4. Verify container health:
   - `docker compose -f scripts/deployment/redis/docker-compose.yml ps`
5. Validate Redis access:
   - `docker exec -it trader-redis redis-cli PING`
6. Validate stream creation (after app bootstrap):
   - `docker exec -it trader-redis redis-cli XINFO STREAM news_raw_queue`

## Shutdown Procedure

- Stop service:
  - `pwsh -File scripts/deployment/redis/stop.ps1`
  - `bash scripts/deployment/redis/stop.sh`

## Data Persistence

- Stream data is persisted in Docker volume `redis_data`.
- Append-only file mode must be enabled to preserve queued events across restarts.
- To reset local state completely:
  - `docker compose -f scripts/deployment/redis/docker-compose.yml down -v`

## Notes

- Keep secrets out of committed files.
- Use `.env` locally and secret management for non-local environments.
- If Redis authentication is enabled, clients must use `REDIS_PASSWORD` consistently.
