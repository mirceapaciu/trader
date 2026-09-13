#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/development.conf" >&2
  exit 2
fi

CONFIG_FILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
# shellcheck source=/dev/null
source "$CONFIG_FILE"

required=(DEV_SERVER_HOST DEV_SERVER_SSH_PORT DEV_SERVER_USER DEV_SERVER_SSH_KEY_PATH EXPECTED_UBUNTU_VERSION GITHUB_REPOSITORY GITHUB_TOKEN_FILE)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" || "${!name}" == \<*\> ]]; then
    echo "Set $name in $CONFIG_FILE" >&2
    exit 2
  fi
done
for file in "$DEV_SERVER_SSH_KEY_PATH" "$GITHUB_TOKEN_FILE"; do
  [[ -f "$file" ]] || { echo "File not found: $file" >&2; exit 2; }
done

REMOTE_STAGE="/tmp/trader-development-bootstrap"
SSH_ARGS=(-i "$DEV_SERVER_SSH_KEY_PATH" -p "$DEV_SERVER_SSH_PORT" -o IdentitiesOnly=yes)
SCP_ARGS=(-i "$DEV_SERVER_SSH_KEY_PATH" -P "$DEV_SERVER_SSH_PORT" -o IdentitiesOnly=yes)
TARGET="$DEV_SERVER_USER@$DEV_SERVER_HOST"

ssh "${SSH_ARGS[@]}" "$TARGET" "rm -rf '$REMOTE_STAGE' && mkdir -m 700 '$REMOTE_STAGE'"
scp "${SCP_ARGS[@]}" -r "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/." "$TARGET:$REMOTE_STAGE/"
scp "${SCP_ARGS[@]}" "$CONFIG_FILE" "$TARGET:$REMOTE_STAGE/development.conf"
scp "${SCP_ARGS[@]}" "$GITHUB_TOKEN_FILE" "$TARGET:$REMOTE_STAGE/github.token"
ssh -t "${SSH_ARGS[@]}" "$TARGET" "chmod 600 '$REMOTE_STAGE/development.conf' '$REMOTE_STAGE/github.token' && sudo bash '$REMOTE_STAGE/bootstrap.sh' '$REMOTE_STAGE/development.conf'"
ssh "${SSH_ARGS[@]}" "$TARGET" "rm -rf '$REMOTE_STAGE'"

echo "Development server bootstrap completed: $DEV_SERVER_HOST"
