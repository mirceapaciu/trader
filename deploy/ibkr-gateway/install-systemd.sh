#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --gateway-version <version> [--mode paper|live] [--api-port <port>]" >&2
  exit 2
}

gateway_version=""
trading_mode="paper"
api_port="4002"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gateway-version)
      gateway_version="${2:-}"
      shift 2
      ;;
    --mode)
      trading_mode="${2:-}"
      shift 2
      ;;
    --api-port)
      api_port="${2:-}"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "Run this installer with sudo." >&2; exit 1; }
[[ "${gateway_version}" =~ ^[0-9]+$ ]] || usage
[[ "${trading_mode}" == "paper" || "${trading_mode}" == "live" ]] || usage
[[ "${api_port}" =~ ^[0-9]+$ ]] || usage
id ibkr >/dev/null

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -o ibkr -g ibkr -m 0750 /home/ibkr/ibc /home/ibkr/ibc/logs
install -o ibkr -g ibkr -m 0750 "${script_dir}/start-ibc-gateway.sh" /home/ibkr/ibc/start-ibc-gateway.sh
install -o root -g root -m 0755 "${script_dir}/ibc-gateway-healthcheck" /usr/local/sbin/ibc-gateway-healthcheck
install -o root -g root -m 0644 "${script_dir}/ibc-gateway.service" /etc/systemd/system/ibc-gateway.service
install -o root -g root -m 0644 "${script_dir}/ibc-gateway-health.service" /etc/systemd/system/ibc-gateway-health.service
install -o root -g root -m 0644 "${script_dir}/ibc-gateway-health.timer" /etc/systemd/system/ibc-gateway-health.timer
printf 'IBKR_GATEWAY_VERSION=%s\nIBKR_TRADING_MODE=%s\nIBKR_API_PORT=%s\n' \
  "${gateway_version}" "${trading_mode}" "${api_port}" > /etc/default/ibc-gateway
chmod 0644 /etc/default/ibc-gateway

systemctl daemon-reload
systemctl enable --now ibc-gateway.service ibc-gateway-health.timer
systemctl status --no-pager ibc-gateway.service ibc-gateway-health.timer
