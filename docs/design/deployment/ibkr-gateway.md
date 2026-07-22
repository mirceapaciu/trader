# IBKR Connection (TWS vs Gateway) Deployment Note

## Purpose

Define how the TradeExecutor (and MarketData) connect to Interactive Brokers, and
which IBKR desktop app to run for automated vs. development use.

## TWS vs IB Gateway

Both apps expose the **same** API socket; `ib_async` connects to either identically.
The difference is what wraps that socket.

| | **IB Gateway** | **TWS (Trader Workstation)** |
|---|---|---|
| Purpose | API-only, headless-friendly | Full trading GUI |
| Footprint | Light (~250 MB) | Heavy (~1 GB+), needs a desktop session |
| Manual trading / charts | No | Yes |
| Best for | The running, unattended service | Development / watching orders land |

**Recommendation:**
- **Run the TradeExecutor service against IB Gateway.** It is lighter, headless-friendly,
  and more stable for a long-lived single-instance consumer.
- **Use TWS on the paper port during development** when you want to *see* the OCA brackets,
  stop/take-profit legs, and fills appear in the GUI to confirm the gateway's behavior.

## Ports and client IDs

`TradeExecutorSettings.validate_mode_port_agreement()` fails closed unless the trading
mode and port agree, so the port — not the flag — determines which account trades.

| App / account | Port | Env |
|---|---|---|
| TWS paper | 7497 | `IBKR_PORT=7497` (default) |
| Gateway paper | 4002 | `IBKR_PORT=4002` |
| TWS live | 7496 | `IBKR_PORT=7496` |
| Gateway live | 4001 | `IBKR_PORT=4001` |

`TRADE_EXECUTOR_TRADING_MODE=paper` (default) must pair with a paper port; `live` requires
both the explicit `live` flag **and** a live port. Any other port is refused at startup.

**Client IDs** (one connection per client ID; multiple clients may share one Gateway):

| Component | Env | Default |
|---|---|---|
| MarketData | `IBKR_MARKET_DATA_CLIENT_ID` | 2 |
| TradeExecutor | `IBKR_TRADE_EXECUTOR_CLIENT_ID` | 5 |

## Gateway setup checklist

1. Log in to **IB Gateway** in the desired mode (paper or live).
2. Configure → Settings → **API → Settings**:
   - Enable **ActiveX and Socket Clients**.
   - Set **Socket port** to match `IBKR_PORT`.
   - Add `127.0.0.1` to **Trusted IPs**.
   - For live trading, **uncheck** "Read-Only API".
3. Confirm market-data subscriptions are active on the account (identical to TWS).

## Unattended operation (24/7)

Both TWS and Gateway **auto-restart daily** and log out, which would leave the executor in
its "disconnected → reconnect with backoff" state until someone logs back in. For a 24/7
deployment, run Gateway under **[IBC (IBController)](https://github.com/IbcAlpha/IBC)** to
auto-relaunch and re-login unattended.

## Linux host systemd deployment

For a production Docker host, run the paper Gateway on the **host** as the dedicated
`ibkr` user. Containers reach it through `IBKR_HOST=host.docker.internal` and
`IBKR_PORT=4002`.

IBC can exit successfully when IBKR performs its daily simulated-trading session shutdown.
The supervisor must therefore restart it on **every** exit, including exit code `0`; a
one-shot process check or a one-time `nohup` launch is not sufficient.

The host artifacts are versioned in [`deploy/ibkr-gateway/`](../../../deploy/ibkr-gateway/):

- `start-ibc-gateway.sh` launches the installed Gateway through IBC and `xvfb-run`.
- `ibc-gateway.service` restarts IBC after both failures and clean daily exits.
- `ibc-gateway-healthcheck`, `ibc-gateway-health.service`, and
  `ibc-gateway-health.timer` restart Gateway when its API port is absent.
- `install-systemd.sh` installs the artifacts at their host paths and enables them.

The unit contents below are a reference for the installed files; deploy them with the
repository installer rather than creating host-local copies by hand.

`/etc/systemd/system/ibc-gateway.service`:

```ini
[Unit]
Description=IBKR paper Gateway controlled by IBC
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ibkr
Group=ibkr
WorkingDirectory=/home/ibkr
Environment=HOME=/home/ibkr
EnvironmentFile=-/etc/default/ibc-gateway
ExecStart=/home/ibkr/ibc/start-ibc-gateway.sh
Restart=always
RestartSec=60
StartLimitIntervalSec=0
TimeoutStopSec=45
KillMode=control-group
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

The `start-ibc-gateway.sh` launcher must stay in the foreground and use `xvfb-run` for the
headless Gateway session. It should invoke IBC in paper mode with the installed Gateway
version, for example:

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${IBKR_GATEWAY_VERSION:?IBKR_GATEWAY_VERSION must be set}"
gateway_path="/home/ibkr/Jts/ibgateway/${IBKR_GATEWAY_VERSION}"

exec /usr/bin/xvfb-run -a -s '-screen 0 1600x1000x24' \
  bash /opt/ibc/scripts/ibcstart.sh "${IBKR_GATEWAY_VERSION}" \
  --gateway \
  --tws-path=/home/ibkr/Jts \
  --ibc-path=/opt/ibc \
  --ibc-ini=/home/ibkr/ibc/config.ini \
  --mode=paper \
  --java-path="${gateway_path}/jre/bin"
```

Add a port health check so a hung Gateway is restarted even if its parent IBC process has
not yet exited.

`/usr/local/sbin/ibc-gateway-healthcheck`:

```bash
#!/usr/bin/env bash
set -euo pipefail

api_port="${IBKR_API_PORT:-4002}"
if ! timeout 5 bash -c "</dev/tcp/127.0.0.1/${api_port}"; then
  systemctl restart ibc-gateway.service
fi
```

`/etc/systemd/system/ibc-gateway-health.service`:

```ini
[Unit]
Description=Verify the IBKR Gateway API port is listening
After=network-online.target ibc-gateway.service

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/ibc-gateway
ExecStart=/usr/local/sbin/ibc-gateway-healthcheck
```

`/etc/systemd/system/ibc-gateway-health.timer`:

```ini
[Unit]
Description=Run the IBKR Gateway API port health check every minute

[Timer]
OnBootSec=90
OnUnitActiveSec=1min
Persistent=true
Unit=ibc-gateway-health.service

[Install]
WantedBy=timers.target
```

Activate the deployment:

```bash
cd /path/to/trader
sudo deploy/ibkr-gateway/install-systemd.sh \
  --gateway-version 1048 \
  --mode paper \
  --api-port 4002
sudo systemctl status ibc-gateway.service ibc-gateway-health.timer
ss -ltnp | grep ':4002'
```

Use the installed Gateway's major version for `--gateway-version` (for example, the directory
name under `/home/ibkr/Jts/ibgateway/`). The installer writes only non-secret runtime settings
to `/etc/default/ibc-gateway`; IBC credentials remain in `/home/ibkr/ibc/config.ini`.

IBC may still need a human to approve a new IBKR two-factor-authentication request. Systemd
can restart the process, but cannot complete that approval.

## Connectivity smoke test

`scripts/deployment/trade-executor/smoke_test.py` is a **read-only** check (connect →
account / positions / open orders / quote → disconnect; **places no orders**). Run it with
TWS/Gateway up in paper mode to confirm the API, mode/port agreement, contract qualification,
and market-data entitlement before starting the service:

```
uv run python scripts/deployment/trade-executor/smoke_test.py [TICKER] [EXCHANGE_MIC]
```

It uses a distinct client id (default 11) so it never clashes with the live MarketData (2) /
TradeExecutor (5) connections. A `quote ... unavailable` line with everything else succeeding
means the connection is fine but the account lacks a real-time subscription for that instrument.

For a production Docker container, use the packaged diagnostic module instead
of the repo-local script:

```
docker exec trader-thesis-builder-1 .venv/bin/python -m src.product_components.diagnostics.ibkr_connectivity
```

It first checks the configured TCP socket, then performs a read-only IB API
handshake using a diagnostic client id. Add `--tcp-only` to skip the API
handshake and test just the Docker-to-Gateway socket path.

To verify the full market-data path after the Gateway starts, request one non-trading quote
through the ThesisBuilder container's `MarketDataService`, then confirm that
`market_data.t_market_data_fetch_runs` and `market_data.t_market_quotes` contain a recent
`provider=ibkr` row. A `data_type=delayed` result is expected when
`MARKET_DATA_ALLOW_DELAYED=true` and the account has no real-time entitlement.

## Operational troubleshooting

```bash
sudo systemctl status ibc-gateway.service ibc-gateway-health.timer
sudo journalctl -u ibc-gateway.service -n 100 --no-pager
ss -ltnp | grep ':4002'
docker exec trader-thesis-builder-1 .venv/bin/python -m src.product_components.diagnostics.ibkr_connectivity
```

If port `4002` is absent, investigate the IBC journal before restarting the Docker services.
The failure is on the host-side Gateway path, not in ThesisBuilder. If the diagnostic connects
but a quote is unavailable, check the account's market-data entitlement; a delayed quote can
still be valid when delayed data is enabled.

## Notes

- The concrete broker adapter lives in
  `src/product_components/trade_executor/broker/ib_async_gateway.py` (the only module that
  imports `ib_async`); everything else depends on the `BrokerGateway` Protocol.
- The adapter cannot be exercised without a running TWS/Gateway; unit tests use the in-memory
  fake gateway instead. The smoke test above is the manual path to validate the real adapter.
