#!/usr/bin/env bash
set -euo pipefail
source "${1:?configuration file required}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
REPO_DIR="$TRADER_DEV_ROOT/repository"
cd "$REPO_DIR"
uv sync --frozen
npm --prefix src/product_components/monitoring_ui/frontend ci

