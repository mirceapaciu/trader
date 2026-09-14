#!/usr/bin/env bash
set -euo pipefail
source "${1:?configuration file required}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
REPO_DIR="$TRADER_DEV_ROOT/repository"
for command in bwrap git uv node npm docker gh; do command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 2; }; done
command -v codex >/dev/null || { echo "Codex CLI is missing" >&2; exit 2; }
if ! bwrap --unshare-user --unshare-net --ro-bind / / --dev /dev --proc /proc -- /usr/bin/true; then
  echo "Bubblewrap could not create the user/network namespaces required by the Codex Linux sandbox" >&2
  exit 2
fi
uv run --project "$REPO_DIR" python -c 'import sys; assert sys.version_info >= (3, 14), sys.version'
docker compose -f "$REPO_DIR/scripts/deployment/postgres/docker-compose.yml" ps --status running --services | grep -qx postgres
docker compose -f "$REPO_DIR/scripts/deployment/redis/docker-compose.yml" ps --status running --services | grep -qx redis
cd "$REPO_DIR"
uv run pytest -m "not integration" -q
uv run pytest -m "integration and not llm_eval" -q
npm --prefix src/product_components/monitoring_ui/frontend run build
echo "Development environment is ready."
