#!/usr/bin/env bash
set -euo pipefail
source "${1:?configuration file required}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
REPO_DIR="$TRADER_DEV_ROOT/repository"
POSTGRES_ENV="$REPO_DIR/scripts/deployment/postgres/.env"
REDIS_ENV="$REPO_DIR/scripts/deployment/redis/.env"

if [[ ! -f "$POSTGRES_ENV" ]]; then
  DB_PASSWORD="$(openssl rand -hex 24)"
  sed "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$DB_PASSWORD/" "$REPO_DIR/scripts/deployment/postgres/.env.example" > "$POSTGRES_ENV"
  chmod 600 "$POSTGRES_ENV"
fi
if [[ ! -f "$REDIS_ENV" ]]; then
  cp "$REPO_DIR/scripts/deployment/redis/.env.example" "$REDIS_ENV"
  chmod 600 "$REDIS_ENV"
fi

cp -n "$REPO_DIR/.env.shared.example" "$REPO_DIR/.env.shared" || true
cp -n "$REPO_DIR/.env.test.example" "$REPO_DIR/.env.test" || true
DB_PASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$POSTGRES_ENV")"
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$DB_PASSWORD/" "$REPO_DIR/.env.shared"

docker compose -f "$REPO_DIR/scripts/deployment/postgres/docker-compose.yml" up -d
docker compose -f "$REPO_DIR/scripts/deployment/redis/docker-compose.yml" up -d

