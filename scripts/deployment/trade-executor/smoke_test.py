"""Read-only IBKR connectivity smoke test for the TradeExecutor gateway.

Exercises the real ``IbAsyncBrokerGateway`` against a running paper TWS / IB
Gateway: connect -> account / positions / open orders / quote -> disconnect.
It places **no orders** and is safe to run at any time.

Prerequisites:
  - TWS or IB Gateway running in paper mode with the API enabled
    (Configure > Settings > API): "Enable ActiveX and Socket Clients",
    socket port matching IBKR_PORT, 127.0.0.1 trusted.
  - Env files present (.env.shared / .env.trade-executor / .env.secrets).

Usage (from the repo root):
  uv run python scripts/deployment/trade-executor/smoke_test.py [TICKER] [EXCHANGE_MIC]
  uv run python scripts/deployment/trade-executor/smoke_test.py AAPL XNAS

A distinct client id (default 11) is used so the test never clashes with the
live MarketData (2) / TradeExecutor (5) connections.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.trade_executor.broker.ib_async_gateway import IbAsyncBrokerGateway
from src.product_components.trade_executor.settings import TradeExecutorSettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IBKR connectivity smoke test.")
    parser.add_argument("ticker", nargs="?", default="AAPL", help="ticker to quote (default AAPL)")
    parser.add_argument("exchange", nargs="?", default="XNAS", help="exchange MIC (default XNAS)")
    parser.add_argument("--client-id", type=int, default=11, help="IBKR client id for the test")
    parser.add_argument(
        "--currency",
        default="USD",
        help="contract currency for SMART routing (e.g. EUR for German listings)",
    )
    parser.add_argument(
        "--delayed",
        action="store_true",
        help="request delayed data (type 3) instead of real-time — diagnostic only; the "
        "executor requires real-time and rejects stale quotes",
    )
    args = parser.parse_args()

    load_env_files(
        REPO_ROOT,
        filenames=(".env.shared", ".env.prod", ".env.trade-executor", ".env.secrets"),
        override_existing=True,
    )
    settings = TradeExecutorSettings.from_env()
    print(
        f"mode={settings.trading_mode} host={settings.ibkr_host} port={settings.ibkr_port} "
        f"(using client_id={args.client_id} for this test)"
    )
    try:
        settings.validate_mode_port_agreement()
        print("mode/port agreement: OK")
    except Exception as exc:
        print(f"mode/port agreement FAILED: {exc}")
        return 1

    gateway = IbAsyncBrokerGateway(
        host=settings.ibkr_host, port=settings.ibkr_port, client_id=args.client_id,
        default_currency=args.currency,
    )
    try:
        gateway.connect()
    except Exception as exc:
        print(f"CONNECT FAILED: {type(exc).__name__}: {exc}")
        print(
            "Checklist: TWS/Gateway running in paper mode, API enabled, socket port matches "
            "IBKR_PORT, 127.0.0.1 trusted, and accept the incoming-connection prompt."
        )
        return 1
    print(f"connected: {gateway.is_connected()}")

    try:
        positions = gateway.list_positions()
        print(
            f"positions ({len(positions)}): "
            + (", ".join(f"{p.ticker} x{p.quantity}@{p.avg_cost}" for p in positions) or "none")
        )
        print(f"account unrealized PnL total: {gateway.account_snapshot().total_unrealized_pnl}")
        print(f"open orders: {len(gateway.list_open_orders())}")

        if args.delayed:
            # Diagnostic: switch the session feed to delayed (type 3) to confirm the
            # quote path works even without a real-time entitlement for the instrument.
            gateway._call(lambda: gateway._ib.reqMarketDataType(3))
            print("market-data type: DELAYED (diagnostic; not valid for live execution)")

        quote = gateway.snapshot_quote(
            ticker=args.ticker, exchange_code=args.exchange, timeout_seconds=10.0
        )
        if quote is None:
            print(
                f"quote {args.ticker}: no ticker returned — check the market-data subscription "
                "for this instrument (the executor requires a real-time US feed)."
            )
        else:
            print(
                f"quote {args.ticker}: bid={quote.bid} ask={quote.ask} last={quote.last} "
                f"two_sided={quote.has_two_sided}"
            )
            if not quote.has_two_sided:
                print(
                    "  note: no live bid/ask (market likely closed, or delayed feed) — the "
                    "executor needs a two-sided real-time quote to size and price an order."
                )
    finally:
        gateway.disconnect()
        print("disconnected. Smoke test complete (no orders placed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
