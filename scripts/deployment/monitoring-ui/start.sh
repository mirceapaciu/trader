#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/src/product_components/monitoring_ui/frontend"
LOGS_DIR="$REPO_ROOT/logs"

BACKEND_PORT="${1:-8090}"
FRONTEND_PORT="${2:-5174}"
API_BASE_URL="http://127.0.0.1:${BACKEND_PORT}"
UI_URL="http://127.0.0.1:${FRONTEND_PORT}"
BACKEND_LOG="$LOGS_DIR/monitoring-ui-backend.log"
FRONTEND_LOG="$LOGS_DIR/monitoring-ui-frontend.log"
BACKEND_PID_FILE="$LOGS_DIR/monitoring-ui-backend.pid"
FRONTEND_PID_FILE="$LOGS_DIR/monitoring-ui-frontend.pid"

mkdir -p "$LOGS_DIR"
printf '[%s] starting backend on %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$API_BASE_URL" >>"$BACKEND_LOG"
printf '[%s] starting frontend on %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$UI_URL" >>"$FRONTEND_LOG"

cd "$REPO_ROOT"
nohup env UI_PORT="$BACKEND_PORT" uv run python -m src.product_components.monitoring_ui.backend >>"$BACKEND_LOG" 2>&1 &
printf '%s\n' "$!" >"$BACKEND_PID_FILE"

cd "$FRONTEND_DIR"
if [ ! -d node_modules ]; then
  npm install >>"$FRONTEND_LOG" 2>&1
fi
nohup env VITE_UI_API_BASE_URL="$API_BASE_URL" npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" >>"$FRONTEND_LOG" 2>&1 &
printf '%s\n' "$!" >"$FRONTEND_PID_FILE"

printf 'Monitoring UI backend starting on %s\n' "$API_BASE_URL"
printf 'Monitoring UI frontend starting on %s\n' "$UI_URL"
printf 'Backend log: %s\n' "$BACKEND_LOG"
printf 'Frontend log: %s\n' "$FRONTEND_LOG"
printf 'Backend PID file: %s\n' "$BACKEND_PID_FILE"
printf 'Frontend PID file: %s\n' "$FRONTEND_PID_FILE"
printf 'Tail backend log: tail -f %s\n' "$BACKEND_LOG"
printf 'Tail frontend log: tail -f %s\n' "$FRONTEND_LOG"
printf 'Stop script: scripts/deployment/monitoring-ui/stop.sh\n'
printf 'Open %s in your browser.\n' "$UI_URL"
