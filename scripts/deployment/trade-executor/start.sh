#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOGS_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOGS_DIR/trade-executor.log"
PID_FILE="$LOGS_DIR/trade-executor.pid"

mkdir -p "$LOGS_DIR"
cd "$REPO_ROOT"

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
  uv sync
fi

printf '[%s] starting trade-executor\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
nohup "$REPO_ROOT/.venv/bin/python" -m src.product_components.trade_executor >/dev/null 2>&1 &
echo "$!" > "$PID_FILE"

echo "TradeExecutor starting."
echo "Log: $LOG_FILE"
echo "PID file: $PID_FILE"
echo "Tail log: tail -f '$LOG_FILE'"
