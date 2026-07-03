"""Broker abstraction — the ib_insync isolation seam.

Every module in the component depends only on the DTOs and the ``BrokerGateway``
Protocol defined here. The single concrete ``ib_insync``-backed implementation
lives in ``ib_insync_gateway.py``; tests use ``fake_gateway.InMemoryBrokerGateway``.
Nothing here imports ib_insync, so the service and pipeline stay network-free and
fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class BrokerEventKind(StrEnum):
    ORDER_STATUS = "order_status"
    EXEC_DETAIL = "exec_detail"
    COMMISSION_REPORT = "commission_report"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class QuoteSnapshot:
    ticker: str
    exchange_code: str
    bid: float | None
    ask: float | None
    last: float | None
    as_of: datetime

    def is_fresh(self, *, now: datetime, max_age_seconds: float) -> bool:
        return (now - self.as_of).total_seconds() <= max_age_seconds

    @property
    def has_two_sided(self) -> bool:
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
        )


@dataclass(frozen=True)
class BracketRequest:
    ticker: str
    exchange_code: str
    side: str  # "buy" | "sell"
    quantity: int
    entry_limit_price: float
    stop_price: float
    take_profit_price: float
    oca_group: str
    outside_rth: bool


@dataclass(frozen=True)
class BracketHandle:
    entry_order_id: int
    stop_order_id: int
    take_profit_order_id: int
    oca_group: str


@dataclass(frozen=True)
class FlattenRequest:
    ticker: str
    exchange_code: str
    side_to_close: str  # side of the resulting closing order: "buy" | "sell"
    quantity: int
    limit_price: float
    outside_rth: bool


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    exchange_code: str
    quantity: int  # signed: positive long, negative short
    avg_cost: float


@dataclass(frozen=True)
class OpenOrder:
    ibkr_order_id: int
    ticker: str
    exchange_code: str
    leg_role: str | None
    status: str
    oca_group: str | None


@dataclass(frozen=True)
class AccountSnapshot:
    unrealized_pnl_by_instrument: dict[tuple[str, str], float] = field(default_factory=dict)
    total_unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class BrokerEvent:
    """Normalized broker callback, drained synchronously by the service loop."""

    kind: BrokerEventKind
    ts: datetime
    ibkr_order_id: int | None = None
    ticker: str | None = None
    exchange_code: str | None = None
    status: str | None = None
    filled_qty: float | None = None
    avg_fill_price: float | None = None
    commission: float | None = None
    realized_pnl: float | None = None


class BrokerGateway(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool: ...

    def snapshot_quote(
        self, *, ticker: str, exchange_code: str, timeout_seconds: float
    ) -> QuoteSnapshot | None: ...

    def submit_bracket(self, request: BracketRequest) -> BracketHandle: ...

    def cancel_order(self, ibkr_order_id: int) -> None: ...

    def replace_order_price(self, ibkr_order_id: int, new_limit_price: float) -> None: ...

    def submit_flatten(self, request: FlattenRequest) -> BracketHandle: ...

    def list_open_orders(self) -> list[OpenOrder]: ...

    def list_positions(self) -> list[BrokerPosition]: ...

    def account_snapshot(self) -> AccountSnapshot: ...

    def drain_events(self) -> list[BrokerEvent]: ...
