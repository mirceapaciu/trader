"""Hand-written in-memory fakes for TradeExecutor service tests (no network/DB)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.product_components.trade_executor.models import SignalMessage
from src.product_components.trade_executor.repository import (
    DailyRiskRecord,
    DecisionRecord,
    ExecutionRecord,
    ExposureEntry,
    PositionRecord,
)


@dataclass
class FakeWatchlistRecord:
    is_active: bool = True


class FakeWatchlistReader:
    def __init__(self, *, active: bool = True, present: bool = True) -> None:
        self._active = active
        self._present = present

    def get_watchlist_record(self, *, ticker: str, exchange_code: str):
        if not self._present:
            return None
        return FakeWatchlistRecord(is_active=self._active)


class FakeReviewReader:
    def __init__(self, state: str | None = "approved") -> None:
        self._state = state

    def get_review_state(self, *, card_id: str) -> str | None:
        return self._state


class FakeSectorReader:
    def __init__(self, sector: str | None = None) -> None:
        self._sector = sector

    def get_sector(self, *, ticker: str, exchange_code: str) -> str | None:
        return self._sector


@dataclass
class FakeContext:
    atr_20d: float | None
    current_price: float | None = None
    recent_high_20d: float | None = None
    recent_low_20d: float | None = None


class FakeMarketContextClient:
    def __init__(self, context: FakeContext | None) -> None:
        self._context = context

    def get_market_context(self, *, ticker: str, exchange_code: str, refresh_if_stale: bool = True):
        return self._context


class FakeCalendar:
    """Deterministic calendar: N calendar days ahead, always RTH unless told."""

    def __init__(self, *, rth: bool = True) -> None:
        self._rth = rth

    def time_exit_at(self, *, fill_time: datetime, trading_days: int) -> datetime:
        return fill_time + timedelta(days=trading_days)

    def is_rth(self, now: datetime) -> bool:
        return self._rth

    def next_session_open(self, now: datetime) -> datetime:
        return now


class FakeRedisIo:
    def __init__(self, messages: list[SignalMessage] | None = None) -> None:
        self._messages = list(messages or [])
        self.acked: list[str] = []
        self.dlq: list[tuple[str, str]] = []
        self.delivery_counts: dict[str, int] = {}
        self.bootstrapped = False

    def ping(self) -> bool:
        return True

    def ensure_streams_and_group(self) -> None:
        self.bootstrapped = True

    def read(self, *, count: int, block_ms: int) -> list[SignalMessage]:
        taken = self._messages[:count]
        self._messages = self._messages[count:]
        return taken

    def ack(self, message_id: str) -> None:
        self.acked.append(message_id)

    def delivery_count(self, message_id: str) -> int:
        return self.delivery_counts.get(message_id, 1)

    def publish_dlq(self, *, message: SignalMessage, error_code: str) -> None:
        self.dlq.append((message.message_id, error_code))

    def stream_length(self) -> int | None:
        return len(self._messages)

    def pending_count(self) -> int | None:
        return 0


class FakeRepository:
    """In-memory implementation of the repository surface the service uses."""

    def __init__(self) -> None:
        self._decisions: dict[int, dict[str, Any]] = {}
        self._by_card: dict[str, int] = {}
        self._executions: list[dict[str, Any]] = []
        self._positions: list[dict[str, Any]] = []
        self._daily: dict[date, dict[str, Any]] = {}
        self._next_decision = 1
        self._next_exec = 1
        self._next_position = 1

    # decisions
    def insert_decision(self, record: DecisionRecord) -> int | None:
        if record.thesis_card_id in self._by_card:
            return None
        decision_id = self._next_decision
        self._next_decision += 1
        data = record.__dict__.copy()
        data["id"] = decision_id
        self._decisions[decision_id] = data
        self._by_card[record.thesis_card_id] = decision_id
        return decision_id

    def get_decision(self, decision_id: int) -> DecisionRecord | None:
        data = self._decisions.get(decision_id)
        return DecisionRecord(**data) if data else None

    def list_passed_decisions_without_executions(self) -> list[DecisionRecord]:
        with_exec = {e["decision_id"] for e in self._executions}
        return [
            DecisionRecord(**data)
            for data in self._decisions.values()
            if data["risk_check_passed"] and data["id"] not in with_exec
        ]

    def mark_decision_orphaned(self, decision_id: int) -> None:
        self._decisions[decision_id]["risk_check_details"] = "decision_orphaned"

    # executions
    def insert_execution_leg(self, *, decision_id, leg_role, ibkr_order_id, ibkr_oca_group, status) -> int:
        exec_id = self._next_exec
        self._next_exec += 1
        self._executions.append(
            {
                "id": exec_id, "decision_id": decision_id, "leg_role": str(leg_role),
                "ibkr_order_id": ibkr_order_id, "ibkr_oca_group": ibkr_oca_group,
                "status": status, "fill_price": None, "fill_quantity": None, "commission": None,
            }
        )
        return exec_id

    def get_execution_by_order_id(self, ibkr_order_id: int) -> ExecutionRecord | None:
        for row in reversed(self._executions):
            if row["ibkr_order_id"] == ibkr_order_id:
                return ExecutionRecord(
                    id=row["id"], decision_id=row["decision_id"], leg_role=row["leg_role"],
                    ibkr_order_id=row["ibkr_order_id"], ibkr_oca_group=row["ibkr_oca_group"],
                    status=row["status"], fill_price=row["fill_price"],
                    fill_quantity=row["fill_quantity"], commission=row["commission"],
                )
        return None

    def update_execution(self, *, ibkr_order_id, status=None, fill_price=None, fill_quantity=None,
                         commission=None, executed_at=None, error_message=None) -> None:
        for row in self._executions:
            if row["ibkr_order_id"] == ibkr_order_id:
                if status is not None:
                    row["status"] = status
                if fill_price is not None:
                    row["fill_price"] = fill_price
                if fill_quantity is not None:
                    row["fill_quantity"] = fill_quantity
                if commission is not None:
                    row["commission"] = (row["commission"] or 0.0) + commission

    # positions
    def open_position(self, *, thesis_card_id, decision_id, ticker, exchange_code, side, quantity,
                      avg_entry_price, stop_price, take_profit_price, time_exit_at, opened_at) -> int:
        pos_id = self._next_position
        self._next_position += 1
        self._positions.append(
            {
                "id": pos_id, "thesis_card_id": thesis_card_id, "decision_id": decision_id,
                "ticker": ticker, "exchange_code": exchange_code, "side": str(side),
                "quantity": quantity, "avg_entry_price": avg_entry_price, "stop_price": stop_price,
                "take_profit_price": take_profit_price, "time_exit_at": time_exit_at,
                "opened_at": opened_at, "closed_at": None, "realized_pnl": None, "exit_reason": None,
            }
        )
        return pos_id

    def update_position(self, *, position_id, quantity=None, avg_entry_price=None, time_exit_at=None) -> None:
        for row in self._positions:
            if row["id"] == position_id:
                if quantity is not None:
                    row["quantity"] = quantity
                if avg_entry_price is not None:
                    row["avg_entry_price"] = avg_entry_price
                if time_exit_at is not None:
                    row["time_exit_at"] = time_exit_at

    def close_position(self, *, position_id, exit_reason, realized_pnl, closed_at) -> None:
        for row in self._positions:
            if row["id"] == position_id:
                row["closed_at"] = closed_at
                row["exit_reason"] = str(exit_reason)
                row["realized_pnl"] = realized_pnl

    def _to_record(self, row: dict[str, Any]) -> PositionRecord:
        return PositionRecord(
            id=row["id"], thesis_card_id=row["thesis_card_id"], decision_id=row["decision_id"],
            ticker=row["ticker"], exchange_code=row["exchange_code"], side=row["side"],
            quantity=row["quantity"], avg_entry_price=row["avg_entry_price"],
            stop_price=row["stop_price"], take_profit_price=row["take_profit_price"],
            time_exit_at=row["time_exit_at"], opened_at=row["opened_at"],
            closed_at=row["closed_at"], realized_pnl=row["realized_pnl"], exit_reason=row["exit_reason"],
        )

    def get_position_by_id(self, position_id: int) -> PositionRecord | None:
        for row in self._positions:
            if row["id"] == position_id:
                return self._to_record(row)
        return None

    def get_open_position_by_instrument(self, *, ticker, exchange_code) -> PositionRecord | None:
        for row in self._positions:
            if row["ticker"] == ticker and row["exchange_code"] == exchange_code and row["closed_at"] is None:
                return self._to_record(row)
        return None

    def get_open_position_by_decision(self, decision_id: int) -> PositionRecord | None:
        for row in self._positions:
            if row["decision_id"] == decision_id and row["closed_at"] is None:
                return self._to_record(row)
        return None

    def list_open_positions(self) -> list[PositionRecord]:
        return [self._to_record(r) for r in self._positions if r["closed_at"] is None]

    def list_open_positions_past_time_exit(self, now: datetime) -> list[PositionRecord]:
        return [
            self._to_record(r)
            for r in self._positions
            if r["closed_at"] is None and r["time_exit_at"] is not None and r["time_exit_at"] <= now
        ]

    # exposure / portfolio
    def has_open_or_working_for_instrument(self, *, ticker, exchange_code) -> bool:
        for row in self._positions:
            if row["ticker"] == ticker and row["exchange_code"] == exchange_code and row["closed_at"] is None:
                return True
        for e in self._executions:
            if e["leg_role"] == "entry" and e["status"] == "submitted":
                d = self._decisions.get(e["decision_id"])
                if d and d["ticker"] == ticker and d["exchange_code"] == exchange_code:
                    return True
        return False

    def _working_entries(self) -> list[dict[str, Any]]:
        return [e for e in self._executions if e["leg_role"] == "entry" and e["status"] == "submitted"]

    def portfolio_totals(self) -> tuple[int, float]:
        open_positions = [r for r in self._positions if r["closed_at"] is None]
        open_count = len(open_positions)
        open_notional = sum((r["quantity"] or 0) * (r["avg_entry_price"] or 0) for r in open_positions)
        working = self._working_entries()
        working_notional = 0.0
        for e in working:
            d = self._decisions.get(e["decision_id"])
            if d:
                working_notional += (d["quantity"] or 0) * (d["entry_price"] or 0)
        return open_count + len(working), float(open_notional) + float(working_notional)

    def list_exposure_entries(self) -> list[ExposureEntry]:
        entries: list[ExposureEntry] = []
        for r in self._positions:
            if r["closed_at"] is None:
                entries.append(ExposureEntry(r["ticker"], r["exchange_code"],
                                             (r["quantity"] or 0) * (r["avg_entry_price"] or 0)))
        for e in self._working_entries():
            d = self._decisions.get(e["decision_id"])
            if d:
                entries.append(ExposureEntry(d["ticker"], d["exchange_code"],
                                             (d["quantity"] or 0) * (d["entry_price"] or 0)))
        return entries

    # daily risk
    def _daily_row(self, trade_date: date) -> dict[str, Any]:
        return self._daily.setdefault(
            trade_date, {"trade_date": trade_date, "realized_pnl": 0.0, "trades_count": 0, "halted": False}
        )

    def get_or_create_daily_risk(self, trade_date: date) -> DailyRiskRecord:
        row = self._daily_row(trade_date)
        return DailyRiskRecord(trade_date, row["realized_pnl"], row["trades_count"], row["halted"])

    def increment_trades_count(self, trade_date: date) -> None:
        self._daily_row(trade_date)["trades_count"] += 1

    def add_realized_pnl(self, trade_date: date, delta: float) -> None:
        self._daily_row(trade_date)["realized_pnl"] += delta

    def set_halted(self, trade_date: date) -> None:
        self._daily_row(trade_date)["halted"] = True


def utc(y=2026, mo=7, d=3, h=15, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
