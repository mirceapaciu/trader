#!/usr/bin/env bash
set -euo pipefail
command -v codex >/dev/null || { echo "Codex CLI is not installed; run bootstrap first" >&2; exit 2; }
codex login --device-auth
codex login status
