#!/usr/bin/env bash
set -euo pipefail

: "${IBKR_GATEWAY_VERSION:?IBKR_GATEWAY_VERSION must be set in /etc/default/ibc-gateway}"

ibkr_home="${IBKR_HOME:-/home/ibkr}"
ibc_path="${IBC_PATH:-/opt/ibc}"
trading_mode="${IBKR_TRADING_MODE:-paper}"
gateway_path="${ibkr_home}/Jts/ibgateway/${IBKR_GATEWAY_VERSION}"

if [[ ! -x "${gateway_path}/jre/bin/java" ]]; then
  echo "IB Gateway Java runtime is missing: ${gateway_path}/jre/bin/java" >&2
  exit 1
fi

exec /usr/bin/xvfb-run -a -s '-screen 0 1600x1000x24' \
  bash "${ibc_path}/scripts/ibcstart.sh" "${IBKR_GATEWAY_VERSION}" \
  --gateway \
  --tws-path="${ibkr_home}/Jts" \
  --ibc-path="${ibc_path}" \
  --ibc-ini="${ibkr_home}/ibc/config.ini" \
  --mode="${trading_mode}" \
  --java-path="${gateway_path}/jre/bin"
