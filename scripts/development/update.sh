#!/usr/bin/env bash
set -euo pipefail
source "${1:-/opt/trader-dev/development.conf}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
REPO_DIR="$TRADER_DEV_ROOT/repository"
TOKEN="$(tr -d '\r\n' < "$TRADER_DEV_ROOT/secrets/github.token")"
git -C "$REPO_DIR" -c http.extraHeader="Authorization: Bearer $TOKEN" fetch origin main
git -C "$REPO_DIR" checkout main
git -C "$REPO_DIR" pull --ff-only origin main
"$TRADER_DEV_ROOT/setup-workspace.sh" "$TRADER_DEV_ROOT/development.conf"
"$TRADER_DEV_ROOT/setup-services.sh" "$TRADER_DEV_ROOT/development.conf"
"$TRADER_DEV_ROOT/verify.sh" "$TRADER_DEV_ROOT/development.conf"

