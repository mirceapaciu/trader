"""In-memory BrokerGateway used by tests — no asyncio, no network."""

from __future__ import annotations

from datetime import datetime, timezone

from .gateway import (
    AccountSnapshot,
    BracketHandle,
    BracketRequest,
    BrokerEvent,
    BrokerPosition,
    FlattenRequest,
    OpenOrder,
    QuoteSnapshot,
)


class InMemoryBrokerGateway:
    """Deterministic fake. Tests preset quotes/positions and enqueue events."""

    def __init__(self) -> None:
        self._connected = True
        self._next_order_id = 1000
        self.submitted_brackets: list[tuple[BracketRequest, BracketHandle]] = []
        self.submitted_flattens: list[tuple[FlattenRequest, int]] = []
        self.cancelled_order_ids: list[int] = []
        self.repriced: list[tuple[int, float]] = []
        self._quotes: dict[tuple[str, str], QuoteSnapshot] = {}
        self._positions: list[BrokerPosition] = []
        self._open_orders: list[OpenOrder] = []
        self._account = AccountSnapshot()
        self._pending_events: list[BrokerEvent] = []

    # --- test control helpers -------------------------------------------------

    def set_quote(self, quote: QuoteSnapshot) -> None:
        self._quotes[(quote.ticker, quote.exchange_code)] = quote

    def set_positions(self, positions: list[BrokerPosition]) -> None:
        self._positions = list(positions)

    def set_open_orders(self, orders: list[OpenOrder]) -> None:
        self._open_orders = list(orders)

    def set_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        self._account = snapshot

    def enqueue_event(self, event: BrokerEvent) -> None:
        self._pending_events.append(event)

    def set_connected(self, value: bool) -> None:
        self._connected = value

    def _allocate_order_id(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id

    # --- BrokerGateway Protocol ----------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def snapshot_quote(
        self, *, ticker: str, exchange_code: str, timeout_seconds: float
    ) -> QuoteSnapshot | None:
        return self._quotes.get((ticker, exchange_code))

    def submit_bracket(self, request: BracketRequest) -> BracketHandle:
        handle = BracketHandle(
            entry_order_id=self._allocate_order_id(),
            stop_order_id=self._allocate_order_id(),
            take_profit_order_id=self._allocate_order_id(),
            oca_group=request.oca_group,
        )
        self.submitted_brackets.append((request, handle))
        return handle

    def cancel_order(self, ibkr_order_id: int) -> None:
        self.cancelled_order_ids.append(ibkr_order_id)

    def replace_order_price(self, ibkr_order_id: int, new_limit_price: float) -> None:
        self.repriced.append((ibkr_order_id, new_limit_price))

    def submit_flatten(self, request: FlattenRequest) -> BracketHandle:
        order_id = self._allocate_order_id()
        self.submitted_flattens.append((request, order_id))
        return BracketHandle(
            entry_order_id=order_id,
            stop_order_id=0,
            take_profit_order_id=0,
            oca_group="",
        )

    def list_open_orders(self) -> list[OpenOrder]:
        return list(self._open_orders)

    def list_positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def account_snapshot(self) -> AccountSnapshot:
        return self._account

    def drain_events(self) -> list[BrokerEvent]:
        events = self._pending_events
        self._pending_events = []
        return events


def _now() -> datetime:
    return datetime.now(timezone.utc)
