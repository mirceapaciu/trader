"""Read-only IBKR connectivity smoke test for the MarketData historical gateway.

Exercises the real ``IbAsyncMarketDataGateway`` against a running paper TWS / IB
Gateway: connect -> fetch a small window of historical bars -> disconnect. It places
no orders and is safe to run at any time.

Prerequisites:
  - TWS or IB Gateway running with the API enabled (Configure > Settings > API):
    "Enable ActiveX and Socket Clients", socket port matching IBKR_PORT, 127.0.0.1 trusted.
  - Env files present (.env.shared / .env.market-data / .env.secrets).

Usage (from the repo root):
  uv run python scripts/deployment/market-data/smoke_test.py [TICKER] [EXCHANGE_MIC] [INTERVAL]
  uv run python scripts/deployment/market-data/smoke_test.py AAPL XNAS 1m

A distinct client id (default 12) is used so the test never clashes with the live
MarketData (2) / TradeExecutor (5) / executor-smoke (11) connections.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.product_components.market_data.ibkr_gateway import IbAsyncMarketDataGateway
from src.product_components.market_data.models import MarketDataProvider
from src.product_components.market_data.provider_symbols import default_provider_symbol
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.news_fetcher.env_loader import load_env_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IBKR historical-bars smoke test.")
    parser.add_argument("ticker", nargs="?", default="AAPL", help="ticker to fetch (default AAPL)")
    parser.add_argument("exchange", nargs="?", default="XNAS", help="exchange MIC (default XNAS)")
    parser.add_argument("interval", nargs="?", default="1m", help="bar interval (default 1m)")
    parser.add_argument("--client-id", type=int, default=12, help="IBKR client id for the test")
    parser.add_argument("--days", type=int, default=2, help="lookback window in days (default 2)")
    args = parser.parse_args()

    load_env_files(
        REPO_ROOT,
        filenames=(".env.shared", ".env.prod", ".env.market-data", ".env.secrets"),
        override_existing=True,
    )
    settings = MarketDataSettings.from_env()
    print(f"host={settings.ibkr_host} port={settings.ibkr_port} (using client_id={args.client_id})")

    symbol = default_provider_symbol(
        ticker=args.ticker, exchange_code=args.exchange, provider=MarketDataProvider.IBKR
    )
    gateway = IbAsyncMarketDataGateway(
        host=settings.ibkr_host, port=settings.ibkr_port, client_id=args.client_id
    )
    try:
        gateway.connect()
    except Exception as exc:
        print(f"CONNECT FAILED: {type(exc).__name__}: {exc}")
        print(
            "Checklist: TWS/Gateway running, API enabled, socket port matches IBKR_PORT, "
            "127.0.0.1 trusted, and accept the incoming-connection prompt."
        )
        return 1
    print(f"connected: {gateway.is_connected()}")

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        bars = gateway.historical_bars(
            provider_symbol=symbol.provider_symbol,
            interval=args.interval,
            start=start,
            end=end,
            contract_metadata=symbol.provider_metadata,
        )
        print(f"fetched {len(bars)} {args.interval} bars for {args.ticker} over {args.days}d")
        if bars:
            first, last = bars[0], bars[-1]
            print(f"  first: {first['bar_start_at']} close={first['close']}")
            print(f"  last:  {last['bar_start_at']} close={last['close']}")
        else:
            print(
                "  no bars returned — check the historical-data subscription/entitlement for "
                "this instrument, or that the window overlaps a trading session."
            )
    finally:
        gateway.disconnect()
        print("disconnected. Smoke test complete (no orders placed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
