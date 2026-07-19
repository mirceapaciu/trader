#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"
PROJECT_NAME="${TRADER_COMPOSE_PROJECT:-trader}"
ENV_DIR="${TRADER_ENV_DIR:-$REPO_ROOT/deploy/env}"
UI_PORT="${TRADER_UI_PORT:-8090}"
UI_HEALTH_HOST="${TRADER_UI_HEALTH_HOST:-${TRADER_UI_BIND_IP:-127.0.0.1}}"

required_env_files=(
  ".env.postgres"
  ".env.shared"
  ".env.prod"
  ".env.monitoring-ui"
  ".env.news-fetcher"
  ".env.thesis-builder"
  ".env.secrets"
)

for env_file in "${required_env_files[@]}"; do
  if [[ ! -f "$ENV_DIR/$env_file" ]]; then
    echo "Missing required deployment env file: $ENV_DIR/$env_file" >&2
    exit 2
  fi
done

cd "$REPO_ROOT"

rendered_compose_config="$(docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" config)"
if grep -Eq '^[[:space:]]+IBKR_HOST:[[:space:]]*"?127\.0\.0\.1"?[[:space:]]*$' <<<"$rendered_compose_config"; then
  cat >&2 <<'EOF'
Refusing deployment: rendered Docker Compose config contains IBKR_HOST=127.0.0.1.

In a container, 127.0.0.1 points at the container itself, not the Haas host
running IB Gateway. Set IBKR_HOST=host.docker.internal in the effective
deployment env files, then rerun the deploy.
EOF
  exit 3
fi

docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" build --pull
docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" up -d postgres redis monitoring-ui news-fetcher thesis-builder

if [[ "${TRADER_ENABLE_TRADE_EXECUTOR:-false}" == "true" ]]; then
  docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" --profile trade-executor up -d trade-executor
fi

docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" ps

for _ in {1..30}; do
  if curl --fail --silent "http://${UI_HEALTH_HOST}:${UI_PORT}/api/health" >/dev/null; then
    echo "Monitoring UI health check passed on http://${UI_HEALTH_HOST}:${UI_PORT}/api/health"
    exit 0
  fi
  sleep 2
done

echo "Monitoring UI health check failed; recent logs:" >&2
docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" logs --tail=80 monitoring-ui >&2
exit 1
