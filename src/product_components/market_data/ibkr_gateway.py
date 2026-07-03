"""Interactive Brokers historical-data gateway (the ONLY market_data module importing ib_async).

Mirrors the trade-executor's ``IbAsyncBrokerGateway`` connection pattern: ib_async is
asyncio-based and its ``IB`` object is not thread-safe, so this gateway owns an asyncio
event loop on a dedicated background thread and marshals every RPC onto it via ``_call``.
It runs on its own client id (``IBKR_MARKET_DATA_CLIENT_ID``, default 2), independent of
the trade-executor session, so the two never clash.

Unlike the executor gateway (which only reads live quotes), this one exposes
``historical_bars(...)`` backed by ``reqHistoricalData`` — the source the backtester warms
up from when IBKR is available, falling back to Polygon/Alpha Vantage otherwise.

This module cannot be exercised without a live TWS/Gateway, so it carries no unit tests
beyond the import-boundary check; its pure helpers (interval mapping, duration/date
normalization) are unit-tested directly.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ib_async import IB, Stock

LOGGER = logging.getLogger("market_data.ibkr")

# Interval -> IBKR ``barSizeSetting`` string.
_BAR_SIZE_SETTING: dict[str, str] = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h": "1 hour",
    "1d": "1 day",
}

# Per-interval window fetched by a single ``reqHistoricalData`` call. IBKR caps the duration
# per request by bar size and paces repeated requests, so long ranges are walked backward in
# these chunks. Values are conservative; tune if pacing violations appear in logs.
_CHUNK_DURATION: dict[str, timedelta] = {
    "1m": timedelta(days=1),
    "5m": timedelta(days=5),
    "15m": timedelta(days=14),
    "30m": timedelta(days=28),
    "1h": timedelta(days=28),
    "1d": timedelta(days=365),
}

_REQUEST_TIMEOUT_SECONDS = 60.0
_PACING_SLEEP_SECONDS = 0.25
# Hard cap on chunk iterations per instrument to guarantee termination.
_MAX_CHUNKS = 400


class IbAsyncMarketDataGateway:
    """ib_async-backed gateway that fetches historical OHLCV bars for market_data."""

    def __init__(self, *, host: str, port: int, client_id: int, default_currency: str = "USD") -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._default_currency = default_currency
        self._ib = IB()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._contracts: dict[tuple[str, str, str], Any] = {}  # (symbol, exchange, currency) -> contract

    # --- event loop plumbing -------------------------------------------------

    def _start_loop(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=_run, name="ibkr-market-data-loop", daemon=True)
        self._thread.start()
        ready.wait()

    def _call(self, factory: Callable[[], Any], *, timeout: float | None = None) -> Any:
        """Run an ib_async interaction on the loop thread and block for the result.

        ``factory`` is invoked *on the loop thread* — some ib_async request methods build
        their asyncio.Future eagerly when called, so they must not be created on the caller's
        thread. If the factory returns an awaitable it is awaited; otherwise the plain value
        is returned. Keeps every ib_async touch single-threaded on the loop.
        """
        if self._loop is None:
            raise RuntimeError("IBKR event loop not started")

        async def _runner() -> Any:
            result = factory()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                return await result
            return result

        future: Future = asyncio.run_coroutine_threadsafe(_runner(), self._loop)
        return future.result(timeout=timeout)

    # --- connection ----------------------------------------------------------

    def connect(self) -> None:
        self._start_loop()
        if self._ib.isConnected():
            return
        self._call(
            lambda: self._ib.connectAsync(self._host, self._port, clientId=self._client_id),
            timeout=15.0,
        )

    def disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()

    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    def _qualified_contract(self, provider_symbol: str, contract_metadata: dict[str, Any] | None):
        """Return a conId-qualified Stock contract (cached), or None if unresolvable.

        IBKR requires contracts to be qualified before requesting historical data. The
        exchange / primary_exchange / currency come from the ProviderSymbol metadata built by
        ``provider_symbols._ibkr_symbol``; SMART routing (with a primary exchange hint) is used
        for US listings, and the native exchange for non-US ones.
        """
        meta = contract_metadata or {}
        exchange = str(meta.get("exchange") or "SMART")
        currency = str(meta.get("currency") or self._default_currency)
        primary_exchange = meta.get("primary_exchange")

        key = (provider_symbol, exchange, currency)
        cached = self._contracts.get(key)
        if cached is not None:
            return cached

        contract = Stock(provider_symbol, exchange, currency)
        if primary_exchange:
            contract.primaryExchange = str(primary_exchange)
        try:
            qualified = self._call(
                lambda: self._ib.qualifyContractsAsync(contract), timeout=10.0
            )
        except Exception:
            LOGGER.exception("Contract qualification failed for %s (%s/%s)", provider_symbol, exchange, currency)
            return None
        result = qualified[0] if qualified else None
        if result is not None:
            self._contracts[key] = result
        return result

    # --- historical bars -----------------------------------------------------

    def historical_bars(
        self,
        *,
        provider_symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        contract_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV bars in ``[start, end]`` as raw dicts for ``normalize_ibkr_historical_bars``.

        Best-effort: returns whatever was gathered and never raises into the warmup path. The
        range is walked backward from ``end`` in per-interval chunks; each returned batch's
        earliest bar becomes the next request's end, so the whole window is paged with dedupe.
        """
        bar_size = _BAR_SIZE_SETTING.get(interval)
        if bar_size is None:
            raise ValueError(f"unsupported_ibkr_interval:{interval}")
        start = _to_utc(start)
        end = _to_utc(end)
        if start >= end:
            return []

        contract = self._qualified_contract(provider_symbol, contract_metadata)
        if contract is None:
            return []

        chunk = _CHUNK_DURATION[interval]
        collected: dict[datetime, dict[str, Any]] = {}
        cursor = end
        for _ in range(_MAX_CHUNKS):
            if cursor <= start:
                break
            window_start = max(start, cursor - chunk)
            duration_str = _duration_str(window_start, cursor)
            try:
                raw_bars = self._call(
                    lambda: self._ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime=cursor,
                        durationStr=duration_str,
                        barSizeSetting=bar_size,
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    ),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                LOGGER.exception(
                    "IBKR historical fetch failed for %s %s ending %s", provider_symbol, interval, cursor
                )
                break
            if not raw_bars:
                break

            earliest: datetime | None = None
            for raw in raw_bars:
                ts = _bar_datetime(getattr(raw, "date", None))
                if ts is None:
                    continue
                earliest = ts if earliest is None else min(earliest, ts)
                if start <= ts <= end:
                    collected[ts] = {
                        "bar_start_at": ts,
                        "open": float(raw.open),
                        "high": float(raw.high),
                        "low": float(raw.low),
                        "close": float(raw.close),
                        "volume": float(raw.volume) if getattr(raw, "volume", None) is not None else None,
                    }
            if earliest is None or earliest >= cursor:
                # No forward progress (would loop forever) — stop.
                break
            cursor = earliest
            time.sleep(_PACING_SLEEP_SECONDS)

        return sorted(collected.values(), key=lambda bar: bar["bar_start_at"])


def build_market_data_ibkr_gateway(settings: Any) -> "IbAsyncMarketDataGateway | None":
    """Construct and best-effort connect the market-data IBKR gateway from settings.

    Returns ``None`` when IBKR-first is disabled. On connect failure the (disconnected)
    gateway is still returned: ``is_available()`` reports False so the service transparently
    falls back to Polygon/Alpha Vantage. Callers must ``disconnect()`` it when done.
    """
    if not getattr(settings, "prefer_ibkr_historical", True):
        return None
    gateway = IbAsyncMarketDataGateway(
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_market_data_client_id,
    )
    try:
        gateway.connect()
        LOGGER.info(
            "IBKR market-data session connected (client_id=%s); historical bars prefer IBKR",
            settings.ibkr_market_data_client_id,
        )
    except Exception as exc:
        LOGGER.warning(
            "IBKR unavailable (%s: %s); historical bars fall back to Polygon/Alpha Vantage",
            type(exc).__name__,
            exc,
        )
    return gateway


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _duration_str(start: datetime, end: datetime) -> str:
    """IBKR ``durationStr`` covering ``[start, end]`` ("N S" under a day, else "N D")."""
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        seconds = 60
    if seconds < 86400:
        return f"{seconds} S"
    days = math.ceil(seconds / 86400)
    return f"{days} D"


def _bar_datetime(value: Any) -> datetime | None:
    """Normalize an ib_async ``BarData.date`` (datetime, date, or string) to tz-aware UTC."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
