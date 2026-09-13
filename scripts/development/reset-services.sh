#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "--confirm-delete-development-data" ]]; then
  echo "Usage: $0 --confirm-delete-development-data [configuration-file]" >&2
  exit 2
fi
source "${2:-/opt/trader-dev/development.conf}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
POSTGRES_COMPOSE="$TRADER_DEV_ROOT/repository/scripts/deployment/postgres/docker-compose.yml"
REDIS_COMPOSE="$TRADER_DEV_ROOT/repository/scripts/deployment/redis/docker-compose.yml"
docker compose -f "$POSTGRES_COMPOSE" down --volumes
docker compose -f "$REDIS_COMPOSE" down --volumes
"$TRADER_DEV_ROOT/setup-services.sh" "$TRADER_DEV_ROOT/development.conf"

