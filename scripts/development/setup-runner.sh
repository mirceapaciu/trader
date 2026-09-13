#!/usr/bin/env bash
set -euo pipefail
source "${1:?configuration file required}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY%.git}"
LABELS="${2:-trader-dev}"
TRADER_DEV_USER="${TRADER_DEV_USER:-trader-dev}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
RUNNER_DIR="$TRADER_DEV_ROOT/actions-runner"
TOKEN="$(tr -d '\r\n' < "$TRADER_DEV_ROOT/secrets/github.token")"

if [[ ! -x "$RUNNER_DIR/run.sh" ]]; then
  mkdir -p "$RUNNER_DIR"
  chown "$TRADER_DEV_USER:$TRADER_DEV_USER" "$RUNNER_DIR"
  case "$(dpkg --print-architecture)" in
    amd64) RUNNER_ARCH=x64 ;;
    arm64) RUNNER_ARCH=arm64 ;;
    *) echo "Unsupported runner architecture: $(dpkg --print-architecture)" >&2; exit 4 ;;
  esac
  RUNNER_URL="$(curl -fsSL -H "Authorization: Bearer $TOKEN" -H 'X-GitHub-Api-Version: 2022-11-28' "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/runners/downloads" | jq -r --arg arch "$RUNNER_ARCH" '[.[] | select(.os=="linux" and .architecture==$arch)][0].download_url')"
  [[ "$RUNNER_URL" == https://* ]] || { echo "Could not find Linux $RUNNER_ARCH runner download" >&2; exit 4; }
  curl -fsSL "$RUNNER_URL" -o "$RUNNER_DIR/runner.tar.gz"
  tar -xzf "$RUNNER_DIR/runner.tar.gz" -C "$RUNNER_DIR"
  rm "$RUNNER_DIR/runner.tar.gz"
  chown -R "$TRADER_DEV_USER:$TRADER_DEV_USER" "$RUNNER_DIR"
fi

if [[ ! -f "$RUNNER_DIR/.runner" ]]; then
  REG_TOKEN="$(curl -fsSL -X POST -H "Authorization: Bearer $TOKEN" -H 'X-GitHub-Api-Version: 2022-11-28' "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/runners/registration-token" | jq -r .token)"
  sudo -u "$TRADER_DEV_USER" "$RUNNER_DIR/config.sh" --unattended --url "https://github.com/$GITHUB_REPOSITORY" --token "$REG_TOKEN" --name "$(hostname)-trader-dev" --labels "$LABELS" --work "$TRADER_DEV_ROOT/runner-work" --replace
fi
if ! compgen -G '/etc/systemd/system/actions.runner.*.service' >/dev/null; then
  cd "$RUNNER_DIR"
  ./svc.sh install "$TRADER_DEV_USER"
fi
cd "$RUNNER_DIR"
./svc.sh start || systemctl restart 'actions.runner.*.service'
