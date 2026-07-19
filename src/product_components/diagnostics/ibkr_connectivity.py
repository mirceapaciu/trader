"""Read-only IBKR connectivity diagnostic for production containers.

This module is packaged under ``src`` so it is available in the Docker runtime
image. It performs a TCP probe first, then optionally opens the real
TradeExecutor broker adapter for a read-only API handshake.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DIAGNOSTIC_CLIENT_ID = 21


@dataclass(frozen=True)
class ProbeResult:
    host: str
    port: int
    ok: bool
    error: str | None = None


def tcp_probe(host: str, port: int, *, timeout_seconds: float) -> ProbeResult:
    sock = socket.socket()
    sock.settimeout(timeout_seconds)
    try:
        sock.connect((host, port))
    except Exception as exc:
        return ProbeResult(
            host=host,
            port=port,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        sock.close()
    return ProbeResult(host=host, port=port, ok=True)


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return int(value)


def _load_env_files_if_requested(enabled: bool) -> None:
    if not enabled:
        return

    from src.product_components.news_fetcher.env_loader import load_env_files

    load_env_files(
        Path.cwd(),
        filenames=(".env.shared", ".env.prod", ".env.trade-executor", ".env.secrets"),
        override_existing=False,
    )


def _run_api_probe(
    *,
    host: str,
    port: int,
    client_id: int,
    currency: str,
) -> int:
    from src.product_components.trade_executor.broker.ib_async_gateway import IbAsyncBrokerGateway
    from src.product_components.trade_executor.settings import TradeExecutorSettings

    settings = TradeExecutorSettings.from_env()
    print(f"mode={settings.trading_mode}")
    try:
        settings.validate_mode_port_agreement()
        print("mode_port_agreement=OK")
    except Exception as exc:
        print(f"mode_port_agreement=FAILED {type(exc).__name__}: {exc}")
        return 1

    gateway = IbAsyncBrokerGateway(
        host=host,
        port=port,
        client_id=client_id,
        default_currency=currency,
    )
    try:
        gateway.connect()
    except Exception as exc:
        print(f"api_connect=FAILED {type(exc).__name__}: {exc}")
        print(
            "checklist=IB Gateway/TWS running, API enabled, socket port matches "
            "IBKR_PORT, container IP trusted, and incoming connection prompt accepted"
        )
        return 1

    try:
        print(f"api_connected={gateway.is_connected()}")
        positions = gateway.list_positions()
        print(f"positions_count={len(positions)}")
        print(f"open_orders_count={len(gateway.list_open_orders())}")
        snapshot = gateway.account_snapshot()
        print(f"account_unrealized_pnl={snapshot.total_unrealized_pnl}")
    finally:
        gateway.disconnect()
        print("api_disconnected=True")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only IBKR connectivity diagnostic for Docker containers."
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_DIAGNOSTIC_CLIENT_ID,
        help="diagnostic IBKR client id; use one distinct from live services",
    )
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--timeout-seconds", type=float, default=3.0)
    parser.add_argument(
        "--tcp-only",
        action="store_true",
        help="only test whether the socket opens; skip the IB API handshake",
    )
    parser.add_argument(
        "--load-env-files",
        action="store_true",
        help="load repo env files from the current directory before probing; mostly for host-local use",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_env_files_if_requested(args.load_env_files)

    host = args.host or os.getenv("IBKR_HOST", "127.0.0.1")
    port = args.port or _int_env("IBKR_PORT", 7497)
    client_id = args.client_id

    print(f"target={host}:{port}")
    print(f"diagnostic_client_id={client_id}")
    result = tcp_probe(host, port, timeout_seconds=args.timeout_seconds)
    if result.ok:
        print("tcp_connect=OK")
    else:
        print(f"tcp_connect=FAILED {result.error}")
        return 1

    if args.tcp_only:
        print("api_probe=SKIPPED")
        return 0

    return _run_api_probe(
        host=host,
        port=port,
        client_id=client_id,
        currency=args.currency,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
