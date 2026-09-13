#!/usr/bin/env bash
set -euo pipefail
source "${1:-/opt/trader-dev/development.conf}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
docker compose -f "$TRADER_DEV_ROOT/repository/scripts/deployment/postgres/docker-compose.yml" stop
docker compose -f "$TRADER_DEV_ROOT/repository/scripts/deployment/redis/docker-compose.yml" stop

