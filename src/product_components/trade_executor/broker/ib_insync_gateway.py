"""Interactive Brokers execution gateway (the ONLY module importing ib_insync).

Design (see trade-executor/behavior.md §5 and the plan):
ib_insync is asyncio-based and its ``IB`` object is not thread-safe, so this
gateway owns an asyncio event loop on a dedicated background thread. All broker
RPCs from the (single-threaded) service loop are marshalled onto that thread and
awaited with a timeout. IB callbacks run on the loop thread and push *normalized*
``BrokerEvent``s into a lock-guarded deque; the service drains them synchronously
via ``drain_events()``. Nothing in the rest of the component imports ib_insync.

This module cannot be exercised without a live TWS/Gateway, so it carries no unit
tests beyond the import-boundary check; its normalization helpers are kept small
and pure where possible.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import Any, Callable

# eventkit (pulled in by ib_insync) calls asyncio.get_event_loop() at import time,
# which raises on Python 3.14 when no loop exists in the main thread. Ensure one is
# present before importing so the module loads; the gateway runs its real loop on a
# dedicated background thread (see _start_loop).
try:  # pragma: no cover - import-time environment shim
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, LimitOrder, Stock

from .gateway import (
    AccountSnapshot,
    BracketHandle,
    BracketRequest,
    BrokerEvent,
    BrokerEventKind,
    FlattenRequest,
    OpenOrder,
    BrokerPosition,
    QuoteSnapshot,
)

LOGGER = logging.getLogger("trade_executor.broker.ibkr")


class IbInsyncBrokerGateway:
    """Concrete BrokerGateway backed by ib_insync."""

    def __init__(self, *, host: str, port: int, client_id: int, default_currency: str = "USD") -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._default_currency = default_currency
        self._ib = IB()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._events: deque[BrokerEvent] = deque()
        self._events_lock = threading.Lock()
        self._orders: dict[int, Any] = {}  # ibkr_order_id -> Trade
        self._register_callbacks()

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

        self._thread = threading.Thread(target=_run, name="ibkr-loop", daemon=True)
        self._thread.start()
        ready.wait()

    def _call(self, coro_factory: Callable[[], Any], *, timeout: float | None = None) -> Any:
        """Run a coroutine on the loop thread and block for its result."""
        if self._loop is None:
            raise RuntimeError("IBKR event loop not started")
        future: Future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result(timeout=timeout)

    # --- callbacks -----------------------------------------------------------

    def _register_callbacks(self) -> None:
        self._ib.orderStatusEvent += self._on_order_status
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.commissionReportEvent += self._on_commission_report
        self._ib.connectedEvent += self._on_connected
        self._ib.disconnectedEvent += self._on_disconnected

    def _push(self, event: BrokerEvent) -> None:
        with self._events_lock:
            self._events.append(event)

    def _on_order_status(self, trade: Any) -> None:
        status = trade.orderStatus
        self._push(
            BrokerEvent(
                kind=BrokerEventKind.ORDER_STATUS,
                ts=_now(),
                ibkr_order_id=int(trade.order.orderId),
                ticker=getattr(trade.contract, "symbol", None),
                status=str(status.status),
                filled_qty=float(status.filled),
                avg_fill_price=float(status.avgFillPrice) if status.avgFillPrice else None,
            )
        )

    def _on_exec_details(self, trade: Any, fill: Any) -> None:
        self._push(
            BrokerEvent(
                kind=BrokerEventKind.EXEC_DETAIL,
                ts=_now(),
                ibkr_order_id=int(trade.order.orderId),
                ticker=getattr(trade.contract, "symbol", None),
                status=str(trade.orderStatus.status),
                filled_qty=float(fill.execution.cumQty),
                avg_fill_price=float(fill.execution.avgPrice) if fill.execution.avgPrice else None,
            )
        )

    def _on_commission_report(self, trade: Any, fill: Any, report: Any) -> None:
        realized = getattr(report, "realizedPNL", None)
        self._push(
            BrokerEvent(
                kind=BrokerEventKind.COMMISSION_REPORT,
                ts=_now(),
                ibkr_order_id=int(trade.order.orderId),
                commission=float(report.commission) if report.commission is not None else None,
                realized_pnl=float(realized) if realized not in (None, "") else None,
            )
        )

    def _on_connected(self) -> None:
        self._push(BrokerEvent(kind=BrokerEventKind.CONNECTED, ts=_now()))

    def _on_disconnected(self) -> None:
        self._push(BrokerEvent(kind=BrokerEventKind.DISCONNECTED, ts=_now()))

    # --- BrokerGateway Protocol ----------------------------------------------

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

    def _contract(self, ticker: str, exchange_code: str):
        # SMART routing; exchange_code (MIC) is advisory. US names route via SMART.
        return Stock(ticker, "SMART", self._default_currency)

    def snapshot_quote(
        self, *, ticker: str, exchange_code: str, timeout_seconds: float
    ) -> QuoteSnapshot | None:
        contract = self._contract(ticker, exchange_code)

        async def _fetch():
            tickers = await self._ib.reqTickersAsync(contract, regulatorySnapshot=False)
            return tickers[0] if tickers else None

        try:
            tick = self._call(_fetch, timeout=timeout_seconds)
        except Exception:
            LOGGER.exception("Quote snapshot failed for %s", ticker)
            return None
        if tick is None:
            return None
        return QuoteSnapshot(
            ticker=ticker,
            exchange_code=exchange_code,
            bid=_pos_or_none(getattr(tick, "bid", None)),
            ask=_pos_or_none(getattr(tick, "ask", None)),
            last=_pos_or_none(getattr(tick, "last", None)),
            as_of=_now(),
        )

    def submit_bracket(self, request: BracketRequest) -> BracketHandle:
        action = "BUY" if request.side == "buy" else "SELL"
        contract = self._contract(request.ticker, request.exchange_code)
        bracket = self._ib.bracketOrder(
            action,
            request.quantity,
            limitPrice=request.entry_limit_price,
            takeProfitPrice=request.take_profit_price,
            stopLossPrice=request.stop_price,
        )
        for order in bracket:
            order.ocaGroup = request.oca_group
            order.outsideRth = request.outside_rth

        def _place():
            trades = [self._ib.placeOrder(contract, order) for order in bracket]
            return trades

        trades = self._call(_place, timeout=10.0)
        for trade in trades:
            self._orders[int(trade.order.orderId)] = trade
        # ib_insync bracketOrder returns [parent, takeProfit, stopLoss].
        parent, take_profit, stop_loss = trades
        return BracketHandle(
            entry_order_id=int(parent.order.orderId),
            stop_order_id=int(stop_loss.order.orderId),
            take_profit_order_id=int(take_profit.order.orderId),
            oca_group=request.oca_group,
        )

    def cancel_order(self, ibkr_order_id: int) -> None:
        trade = self._orders.get(ibkr_order_id)
        if trade is None:
            return
        self._call(lambda: _as_coro(self._ib.cancelOrder(trade.order)), timeout=5.0)

    def replace_order_price(self, ibkr_order_id: int, new_limit_price: float) -> None:
        trade = self._orders.get(ibkr_order_id)
        if trade is None:
            return
        order = trade.order
        order.lmtPrice = new_limit_price

        def _replace():
            return self._ib.placeOrder(trade.contract, order)

        self._call(lambda: _as_coro(_replace()), timeout=5.0)

    def submit_flatten(self, request: FlattenRequest) -> BracketHandle:
        action = "BUY" if request.side_to_close == "buy" else "SELL"
        contract = self._contract(request.ticker, request.exchange_code)
        order = LimitOrder(action, request.quantity, request.limit_price)
        order.outsideRth = request.outside_rth

        def _place():
            return self._ib.placeOrder(contract, order)

        trade = self._call(lambda: _as_coro(_place()), timeout=5.0)
        order_id = int(trade.order.orderId)
        self._orders[order_id] = trade
        return BracketHandle(entry_order_id=order_id, stop_order_id=0, take_profit_order_id=0, oca_group="")

    def list_open_orders(self) -> list[OpenOrder]:
        trades = self._call(lambda: self._ib.reqAllOpenOrdersAsync(), timeout=10.0)
        result: list[OpenOrder] = []
        for trade in trades or []:
            result.append(
                OpenOrder(
                    ibkr_order_id=int(trade.order.orderId),
                    ticker=getattr(trade.contract, "symbol", ""),
                    exchange_code=getattr(trade.contract, "primaryExchange", "") or "",
                    leg_role=None,
                    status=str(trade.orderStatus.status),
                    oca_group=getattr(trade.order, "ocaGroup", None) or None,
                )
            )
        return result

    def list_positions(self) -> list[BrokerPosition]:
        positions = self._call(lambda: _as_coro(self._ib.positions()), timeout=10.0)
        result: list[BrokerPosition] = []
        for pos in positions or []:
            result.append(
                BrokerPosition(
                    ticker=getattr(pos.contract, "symbol", ""),
                    exchange_code=getattr(pos.contract, "primaryExchange", "") or "",
                    quantity=int(pos.position),
                    avg_cost=float(pos.avgCost),
                )
            )
        return result

    def account_snapshot(self) -> AccountSnapshot:
        items = self._call(lambda: _as_coro(self._ib.portfolio()), timeout=10.0)
        by_instrument: dict[tuple[str, str], float] = {}
        total = 0.0
        for item in items or []:
            key = (getattr(item.contract, "symbol", ""), getattr(item.contract, "primaryExchange", "") or "")
            pnl = float(getattr(item, "unrealizedPNL", 0.0) or 0.0)
            by_instrument[key] = by_instrument.get(key, 0.0) + pnl
            total += pnl
        return AccountSnapshot(unrealized_pnl_by_instrument=by_instrument, total_unrealized_pnl=total)

    def drain_events(self) -> list[BrokerEvent]:
        with self._events_lock:
            events = list(self._events)
            self._events.clear()
        return events


async def _as_coro(value: Any) -> Any:
    """Wrap an already-computed (synchronous ib_insync) value so it can be awaited."""
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pos_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f <= 0:  # NaN or non-positive
        return None
    return f
