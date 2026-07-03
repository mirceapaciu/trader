from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DecisionRecord:
    id: int | None
    thesis_card_id: str
    ticker: str
    exchange_code: str | None
    action: str
    quantity: int | None
    order_type: str | None
    limit_price: float | None
    entry_price: float | None
    stop_price: float | None
    take_profit_price: float | None
    atr_20d: float | None
    risk_amount_usd: float | None
    confidence: float | None
    signal_strength: float | None
    source_analysis_ids: list[int] | None
    risk_check_passed: bool
    risk_check_details: str | None
    decided_at: datetime


@dataclass(frozen=True)
class ExecutionRecord:
    id: int
    decision_id: int
    leg_role: str | None
    ibkr_order_id: int | None
    ibkr_oca_group: str | None
    status: str
    fill_price: float | None
    fill_quantity: int | None
    commission: float | None


@dataclass(frozen=True)
class PositionRecord:
    id: int
    thesis_card_id: str
    decision_id: int
    ticker: str
    exchange_code: str
    side: str
    quantity: int
    avg_entry_price: float | None
    stop_price: float | None
    take_profit_price: float | None
    time_exit_at: datetime | None
    opened_at: datetime
    closed_at: datetime | None
    realized_pnl: float | None
    exit_reason: str | None


@dataclass(frozen=True)
class DailyRiskRecord:
    trade_date: date
    realized_pnl: float
    trades_count: int
    halted: bool


@dataclass(frozen=True)
class ExposureEntry:
    ticker: str
    exchange_code: str
    notional: float


class PostgresTradeExecutorRepository:
    def __init__(self, *, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = _safe_identifier(schema)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)

    # --- decisions -----------------------------------------------------------

    def insert_decision(self, record: DecisionRecord) -> int | None:
        """Insert a decision row. Returns its id, or None on a duplicate card.

        The UNIQUE(thesis_card_id) constraint is the idempotency guard: a
        redelivered card raises UniqueViolation, which we surface as None so the
        caller records a ``duplicate_card`` outcome.
        """
        sql = (
            f"INSERT INTO {self._schema}.t_trade_decisions "
            f"(thesis_card_id, ticker, exchange_code, action, quantity, order_type, "
            f"limit_price, entry_price, stop_price, take_profit_price, atr_20d, "
            f"risk_amount_usd, confidence, signal_strength, source_analysis_ids, "
            f"risk_check_passed, risk_check_details, decided_at) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"RETURNING id"
        )
        params = (
            record.thesis_card_id,
            record.ticker,
            record.exchange_code,
            record.action,
            record.quantity,
            record.order_type,
            record.limit_price,
            record.entry_price,
            record.stop_price,
            record.take_profit_price,
            record.atr_20d,
            record.risk_amount_usd,
            record.confidence,
            record.signal_strength,
            Json(record.source_analysis_ids) if record.source_analysis_ids is not None else None,
            record.risk_check_passed,
            record.risk_check_details,
            _to_utc(record.decided_at),
        )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = int(cur.fetchone()[0])
                conn.commit()
            return new_id
        except psycopg.errors.UniqueViolation:
            return None

    def get_decision(self, decision_id: int) -> DecisionRecord | None:
        sql = f"SELECT * FROM {self._schema}.t_trade_decisions WHERE id = %s"
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (decision_id,))
            row = cur.fetchone()
        return _decision_from_row(row) if row else None

    def list_passed_decisions_without_executions(self) -> list[DecisionRecord]:
        sql = (
            f"SELECT d.* FROM {self._schema}.t_trade_decisions d "
            f"WHERE d.risk_check_passed = TRUE "
            f"AND NOT EXISTS (SELECT 1 FROM {self._schema}.t_trade_executions e "
            f"WHERE e.decision_id = d.id)"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [_decision_from_row(row) for row in rows]

    def mark_decision_orphaned(self, decision_id: int) -> None:
        sql = (
            f"UPDATE {self._schema}.t_trade_decisions "
            f"SET risk_check_details = 'decision_orphaned' WHERE id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (decision_id,))
            conn.commit()

    # --- executions ----------------------------------------------------------

    def insert_execution_leg(
        self,
        *,
        decision_id: int,
        leg_role: str,
        ibkr_order_id: int | None,
        ibkr_oca_group: str | None,
        status: str,
    ) -> int:
        sql = (
            f"INSERT INTO {self._schema}.t_trade_executions "
            f"(decision_id, leg_role, ibkr_order_id, ibkr_oca_group, status) "
            f"VALUES (%s,%s,%s,%s,%s) RETURNING id"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (decision_id, leg_role, ibkr_order_id, ibkr_oca_group, status))
            new_id = int(cur.fetchone()[0])
            conn.commit()
        return new_id

    def get_execution_by_order_id(self, ibkr_order_id: int) -> ExecutionRecord | None:
        sql = (
            f"SELECT * FROM {self._schema}.t_trade_executions "
            f"WHERE ibkr_order_id = %s ORDER BY id DESC LIMIT 1"
        )
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (ibkr_order_id,))
            row = cur.fetchone()
        return _execution_from_row(row) if row else None

    def update_execution(
        self,
        *,
        ibkr_order_id: int,
        status: str | None = None,
        fill_price: float | None = None,
        fill_quantity: int | None = None,
        commission: float | None = None,
        executed_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("fill_price", fill_price),
            ("fill_quantity", fill_quantity),
            ("commission", commission),
            ("executed_at", _to_utc(executed_at) if executed_at else None),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                params.append(value)
        if not assignments:
            return
        params.append(ibkr_order_id)
        sql = (
            f"UPDATE {self._schema}.t_trade_executions SET {', '.join(assignments)} "
            f"WHERE ibkr_order_id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    # --- positions -----------------------------------------------------------

    def open_position(
        self,
        *,
        thesis_card_id: str,
        decision_id: int,
        ticker: str,
        exchange_code: str,
        side: str,
        quantity: int,
        avg_entry_price: float,
        stop_price: float,
        take_profit_price: float,
        time_exit_at: datetime | None,
        opened_at: datetime,
    ) -> int:
        sql = (
            f"INSERT INTO {self._schema}.t_positions "
            f"(thesis_card_id, decision_id, ticker, exchange_code, side, quantity, "
            f"avg_entry_price, stop_price, take_profit_price, time_exit_at, opened_at) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    thesis_card_id, decision_id, ticker, exchange_code, side, quantity,
                    avg_entry_price, stop_price, take_profit_price,
                    _to_utc(time_exit_at) if time_exit_at else None, _to_utc(opened_at),
                ),
            )
            new_id = int(cur.fetchone()[0])
            conn.commit()
        return new_id

    def update_position(
        self,
        *,
        position_id: int,
        quantity: int | None = None,
        avg_entry_price: float | None = None,
        time_exit_at: datetime | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if quantity is not None:
            assignments.append("quantity = %s")
            params.append(quantity)
        if avg_entry_price is not None:
            assignments.append("avg_entry_price = %s")
            params.append(avg_entry_price)
        if time_exit_at is not None:
            assignments.append("time_exit_at = %s")
            params.append(_to_utc(time_exit_at))
        if not assignments:
            return
        params.append(position_id)
        sql = f"UPDATE {self._schema}.t_positions SET {', '.join(assignments)} WHERE id = %s"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    def close_position(
        self,
        *,
        position_id: int,
        exit_reason: str,
        realized_pnl: float | None,
        closed_at: datetime,
    ) -> None:
        sql = (
            f"UPDATE {self._schema}.t_positions "
            f"SET closed_at = %s, exit_reason = %s, realized_pnl = %s WHERE id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (_to_utc(closed_at), exit_reason, realized_pnl, position_id))
            conn.commit()

    def get_position_by_id(self, position_id: int) -> PositionRecord | None:
        return self._fetch_position(f"WHERE id = %s", (position_id,))

    def get_open_position_by_instrument(self, *, ticker: str, exchange_code: str) -> PositionRecord | None:
        return self._fetch_position(
            "WHERE ticker = %s AND exchange_code = %s AND closed_at IS NULL",
            (ticker, exchange_code),
        )

    def get_open_position_by_decision(self, decision_id: int) -> PositionRecord | None:
        return self._fetch_position(
            "WHERE decision_id = %s AND closed_at IS NULL", (decision_id,)
        )

    def list_open_positions(self) -> list[PositionRecord]:
        return self._fetch_positions("WHERE closed_at IS NULL", ())

    def list_open_positions_past_time_exit(self, now: datetime) -> list[PositionRecord]:
        return self._fetch_positions(
            "WHERE closed_at IS NULL AND time_exit_at IS NOT NULL AND time_exit_at <= %s",
            (_to_utc(now),),
        )

    def _fetch_position(self, where: str, params: tuple) -> PositionRecord | None:
        sql = f"SELECT * FROM {self._schema}.t_positions {where} ORDER BY id DESC LIMIT 1"
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _position_from_row(row) if row else None

    def _fetch_positions(self, where: str, params: tuple) -> list[PositionRecord]:
        sql = f"SELECT * FROM {self._schema}.t_positions {where} ORDER BY id"
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_position_from_row(row) for row in rows]

    # --- exposure / portfolio ------------------------------------------------

    def has_open_or_working_for_instrument(self, *, ticker: str, exchange_code: str) -> bool:
        sql = (
            f"SELECT 1 FROM {self._schema}.t_positions "
            f"WHERE ticker = %s AND exchange_code = %s AND closed_at IS NULL "
            f"UNION ALL "
            f"SELECT 1 FROM {self._schema}.t_trade_executions e "
            f"JOIN {self._schema}.t_trade_decisions d ON d.id = e.decision_id "
            f"WHERE e.leg_role = 'entry' AND e.status = 'submitted' "
            f"AND d.ticker = %s AND d.exchange_code = %s "
            f"LIMIT 1"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (ticker, exchange_code, ticker, exchange_code))
            return cur.fetchone() is not None

    def portfolio_totals(self) -> tuple[int, float]:
        """Return (open+working count, deployed+reserved capital)."""
        open_sql = (
            f"SELECT COUNT(*), COALESCE(SUM(quantity * avg_entry_price), 0) "
            f"FROM {self._schema}.t_positions WHERE closed_at IS NULL"
        )
        working_sql = (
            f"SELECT COUNT(*), COALESCE(SUM(d.quantity * d.entry_price), 0) "
            f"FROM {self._schema}.t_trade_executions e "
            f"JOIN {self._schema}.t_trade_decisions d ON d.id = e.decision_id "
            f"WHERE e.leg_role = 'entry' AND e.status = 'submitted'"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(open_sql)
            open_count, open_notional = cur.fetchone()
            cur.execute(working_sql)
            working_count, working_notional = cur.fetchone()
        return (
            int(open_count) + int(working_count),
            float(open_notional) + float(working_notional),
        )

    def list_exposure_entries(self) -> list[ExposureEntry]:
        """Per-instrument notional across open positions + working entries (for sector caps)."""
        open_sql = (
            f"SELECT ticker, exchange_code, quantity * avg_entry_price AS notional "
            f"FROM {self._schema}.t_positions WHERE closed_at IS NULL"
        )
        working_sql = (
            f"SELECT d.ticker, d.exchange_code, d.quantity * d.entry_price AS notional "
            f"FROM {self._schema}.t_trade_executions e "
            f"JOIN {self._schema}.t_trade_decisions d ON d.id = e.decision_id "
            f"WHERE e.leg_role = 'entry' AND e.status = 'submitted'"
        )
        entries: list[ExposureEntry] = []
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            for sql in (open_sql, working_sql):
                cur.execute(sql)
                for row in cur.fetchall():
                    entries.append(
                        ExposureEntry(
                            ticker=str(row["ticker"]),
                            exchange_code=str(row["exchange_code"]),
                            notional=float(row["notional"] or 0.0),
                        )
                    )
        return entries

    # --- daily risk ----------------------------------------------------------

    def get_or_create_daily_risk(self, trade_date: date) -> DailyRiskRecord:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO {self._schema}.t_daily_risk (trade_date) VALUES (%s) "
                f"ON CONFLICT (trade_date) DO NOTHING",
                (trade_date,),
            )
            cur.execute(
                f"SELECT * FROM {self._schema}.t_daily_risk WHERE trade_date = %s",
                (trade_date,),
            )
            row = cur.fetchone()
            conn.commit()
        return DailyRiskRecord(
            trade_date=row["trade_date"],
            realized_pnl=float(row["realized_pnl"]),
            trades_count=int(row["trades_count"]),
            halted=bool(row["halted"]),
        )

    def increment_trades_count(self, trade_date: date) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._schema}.t_daily_risk (trade_date, trades_count) VALUES (%s, 1) "
                f"ON CONFLICT (trade_date) DO UPDATE SET "
                f"trades_count = {self._schema}.t_daily_risk.trades_count + 1",
                (trade_date,),
            )
            conn.commit()

    def add_realized_pnl(self, trade_date: date, delta: float) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._schema}.t_daily_risk (trade_date, realized_pnl) VALUES (%s, %s) "
                f"ON CONFLICT (trade_date) DO UPDATE SET "
                f"realized_pnl = {self._schema}.t_daily_risk.realized_pnl + EXCLUDED.realized_pnl",
                (trade_date, delta),
            )
            conn.commit()

    def set_halted(self, trade_date: date) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._schema}.t_daily_risk (trade_date, halted) VALUES (%s, TRUE) "
                f"ON CONFLICT (trade_date) DO UPDATE SET halted = TRUE",
                (trade_date,),
            )
            conn.commit()


def _decision_from_row(row: dict[str, Any]) -> DecisionRecord:
    return DecisionRecord(
        id=int(row["id"]),
        thesis_card_id=str(row["thesis_card_id"]),
        ticker=str(row["ticker"]),
        exchange_code=row.get("exchange_code"),
        action=str(row["action"]),
        quantity=row.get("quantity"),
        order_type=row.get("order_type"),
        limit_price=row.get("limit_price"),
        entry_price=row.get("entry_price"),
        stop_price=row.get("stop_price"),
        take_profit_price=row.get("take_profit_price"),
        atr_20d=row.get("atr_20d"),
        risk_amount_usd=row.get("risk_amount_usd"),
        confidence=row.get("confidence"),
        signal_strength=row.get("signal_strength"),
        source_analysis_ids=row.get("source_analysis_ids"),
        risk_check_passed=bool(row["risk_check_passed"]),
        risk_check_details=row.get("risk_check_details"),
        decided_at=row["decided_at"],
    )


def _execution_from_row(row: dict[str, Any]) -> ExecutionRecord:
    return ExecutionRecord(
        id=int(row["id"]),
        decision_id=int(row["decision_id"]),
        leg_role=row.get("leg_role"),
        ibkr_order_id=row.get("ibkr_order_id"),
        ibkr_oca_group=row.get("ibkr_oca_group"),
        status=str(row["status"]),
        fill_price=row.get("fill_price"),
        fill_quantity=row.get("fill_quantity"),
        commission=row.get("commission"),
    )


def _position_from_row(row: dict[str, Any]) -> PositionRecord:
    return PositionRecord(
        id=int(row["id"]),
        thesis_card_id=str(row["thesis_card_id"]),
        decision_id=int(row["decision_id"]),
        ticker=str(row["ticker"]),
        exchange_code=str(row["exchange_code"]),
        side=str(row["side"]),
        quantity=int(row["quantity"]),
        avg_entry_price=row.get("avg_entry_price"),
        stop_price=row.get("stop_price"),
        take_profit_price=row.get("take_profit_price"),
        time_exit_at=row.get("time_exit_at"),
        opened_at=row["opened_at"],
        closed_at=row.get("closed_at"),
        realized_pnl=row.get("realized_pnl"),
        exit_reason=row.get("exit_reason"),
    )


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
