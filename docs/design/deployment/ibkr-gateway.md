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

## Notes

- The concrete broker adapter lives in
  `src/product_components/trade_executor/broker/ib_async_gateway.py` (the only module that
  imports `ib_async`); everything else depends on the `BrokerGateway` Protocol.
- The adapter cannot be exercised without a running TWS/Gateway; unit tests use the in-memory
  fake gateway instead. The smoke test above is the manual path to validate the real adapter.
