#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run bootstrap.sh with sudo" >&2; exit 2; }
CONFIG_FILE="${1:-/tmp/trader-development-bootstrap/development.conf}"
# shellcheck source=/dev/null
source "$CONFIG_FILE"

EXPECTED_UBUNTU_VERSION="${EXPECTED_UBUNTU_VERSION:-24.04}"
TRADER_DEV_USER="${TRADER_DEV_USER:-trader-dev}"
TRADER_DEV_ROOT="${TRADER_DEV_ROOT:-/opt/trader-dev}"
RUNNER_LABELS="${GITHUB_RUNNER_LABELS:-trader-dev}"
STAGE_DIR="$(cd "$(dirname "$CONFIG_FILE")" && pwd)"

# shellcheck source=/etc/os-release
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != "$EXPECTED_UBUNTU_VERSION" ]]; then
  echo "Expected Ubuntu $EXPECTED_UBUNTU_VERSION; found ${PRETTY_NAME:-unknown}" >&2
  exit 3
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git jq openssh-client openssl sudo build-essential software-properties-common postgresql-client
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin gh

if ! id "$TRADER_DEV_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$TRADER_DEV_USER"
fi
usermod -aG docker "$TRADER_DEV_USER"
install -d -o "$TRADER_DEV_USER" -g "$TRADER_DEV_USER" -m 0750 "$TRADER_DEV_ROOT" "$TRADER_DEV_ROOT/secrets"
install -o "$TRADER_DEV_USER" -g "$TRADER_DEV_USER" -m 0600 "$STAGE_DIR/github.token" "$TRADER_DEV_ROOT/secrets/github.token"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
if ! command -v codex >/dev/null 2>&1; then
  npm install -g @openai/codex
fi

REPO_DIR="$TRADER_DEV_ROOT/repository"
TOKEN="$(tr -d '\r\n' < "$TRADER_DEV_ROOT/secrets/github.token")"
if [[ ! -d "$REPO_DIR/.git" ]]; then
  sudo -u "$TRADER_DEV_USER" git -c http.extraHeader="Authorization: Bearer $TOKEN" clone "https://github.com/$GITHUB_REPOSITORY.git" "$REPO_DIR"
else
  sudo -u "$TRADER_DEV_USER" git -C "$REPO_DIR" -c http.extraHeader="Authorization: Bearer $TOKEN" fetch origin main
  sudo -u "$TRADER_DEV_USER" git -C "$REPO_DIR" checkout main
  sudo -u "$TRADER_DEV_USER" git -C "$REPO_DIR" pull --ff-only origin main
fi

install -o "$TRADER_DEV_USER" -g "$TRADER_DEV_USER" -m 0755 "$STAGE_DIR"/*.sh "$TRADER_DEV_ROOT/"
install -o "$TRADER_DEV_USER" -g "$TRADER_DEV_USER" -m 0644 "$CONFIG_FILE" "$TRADER_DEV_ROOT/development.conf"

sudo -u "$TRADER_DEV_USER" "$TRADER_DEV_ROOT/setup-workspace.sh" "$TRADER_DEV_ROOT/development.conf"
sudo -u "$TRADER_DEV_USER" "$TRADER_DEV_ROOT/setup-services.sh" "$TRADER_DEV_ROOT/development.conf"
"$TRADER_DEV_ROOT/setup-runner.sh" "$TRADER_DEV_ROOT/development.conf" "$RUNNER_LABELS"
sudo -u "$TRADER_DEV_USER" "$TRADER_DEV_ROOT/verify.sh" "$TRADER_DEV_ROOT/development.conf"
echo "Complete subscription authentication with: sudo -u $TRADER_DEV_USER $TRADER_DEV_ROOT/setup-codex-subscription.sh"
