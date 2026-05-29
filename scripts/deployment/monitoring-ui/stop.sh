#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOGS_DIR="$REPO_ROOT/logs"
BACKEND_PID_FILE="$LOGS_DIR/monitoring-ui-backend.pid"
FRONTEND_PID_FILE="$LOGS_DIR/monitoring-ui-frontend.pid"
BACKEND_LOG="$LOGS_DIR/monitoring-ui-backend.log"
FRONTEND_LOG="$LOGS_DIR/monitoring-ui-frontend.log"

stop_process() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"

  if [ ! -f "$pid_file" ]; then
    printf '%s PID file not found: %s\n' "$name" "$pid_file"
    return
  fi

  local pid
  pid="$(head -n 1 "$pid_file" | tr -d '[:space:]')"

  if [ -z "$pid" ]; then
    rm -f "$pid_file"
    printf '%s PID file was empty and has been removed.\n' "$name"
    return
  fi

  printf '[%s] stopping %s via PID %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$name" "$pid" >>"$log_file"
  kill "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  printf 'Stopped %s (PID %s).\n' "$name" "$pid"
}

stop_process "Monitoring UI backend" "$BACKEND_PID_FILE" "$BACKEND_LOG"
stop_process "Monitoring UI frontend" "$FRONTEND_PID_FILE" "$FRONTEND_LOG"
