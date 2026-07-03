from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .broker.gateway import (
    BracketRequest,
    BrokerEvent,
    BrokerEventKind,
    BrokerGateway,
    FlattenRequest,
)
from .models import (
    DecisionReason,
    ExitReason,
    GateOutcome,
    LegRole,
    PositionSide,
    SignalMessage,
    ThesisCard,
    TradeDirection,
)
from .pipeline import (
    DailyRiskState,
    PortfolioState,
    construct_levels,
    entry_limit_price,
    evaluate_admission_gate,
    evaluate_risk_gate,
    size_position,
)
from .redis_io import RedisTradeExecutorIo
from .repository import DecisionRecord, PostgresTradeExecutorRepository
from .settings import TradeExecutorSettings
from .trading_calendar import TradingCalendar, build_default_calendar

LOGGER = logging.getLogger("trade_executor.service")


class MarketContextClient(Protocol):
    def get_market_context(self, *, ticker: str, exchange_code: str, refresh_if_stale: bool = True):
        ...


class ReviewReader(Protocol):
    def get_review_state(self, *, card_id: str) -> str | None: ...


class WatchlistReader(Protocol):
    def get_watchlist_record(self, *, ticker: str, exchange_code: str): ...


class SectorReader(Protocol):
    def get_sector(self, *, ticker: str, exchange_code: str) -> str | None: ...


@dataclass
class _BracketRef:
    """In-memory tracking for a submitted bracket (rebuilt via reconciliation)."""

    decision_id: int
    card: ThesisCard
    side: str
    quantity: int
    stop_price: float
    take_profit_price: float
    entry_order_id: int
    stop_order_id: int
    take_profit_order_id: int
    entry_limit_price: float
    submitted_at: float
    repriced: bool = False


@dataclass(frozen=True)
class TradeExecutorRuntimeStatus:
    signal_length: int | None
    pending_count: int | None
    open_positions: int
    connected: bool


class TradeExecutorRunner:
    """Operational TradeExecutor consumer + lifecycle manager."""

    def __init__(
        self,
        *,
        settings: TradeExecutorSettings,
        broker: BrokerGateway,
        repository: PostgresTradeExecutorRepository | None = None,
        redis_io: RedisTradeExecutorIo | None = None,
        market_context_client: MarketContextClient | None = None,
        review_reader: ReviewReader | None = None,
        watchlist_reader: WatchlistReader | None = None,
        sector_reader: SectorReader | None = None,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._repository = repository or PostgresTradeExecutorRepository(
            dsn=settings.postgres_dsn, schema=settings.trade_executor_db_schema
        )
        self._redis = redis_io or RedisTradeExecutorIo(
            queue_url=settings.queue_url,
            signal_queue=settings.signal_queue,
            failed_messages_dlq=settings.failed_messages_dlq,
            consumer_group=settings.consumer_group,
            consumer_name=settings.consumer_name,
            claim_min_idle_ms=max(0, settings.claim_min_idle_seconds) * 1000,
        )
        self._market_context = market_context_client
        self._review_reader = review_reader
        self._watchlist_reader = watchlist_reader
        self._sector_reader = sector_reader
        self._calendar = calendar or build_default_calendar()
        self._clock = clock or _utcnow
        self._monotonic = monotonic
        self._tz = ZoneInfo(settings.trading_day_timezone)

        # In-memory lifecycle tracking (durably backed by DB; rebuilt on restart).
        self._brackets: dict[int, _BracketRef] = {}  # decision_id -> bracket
        self._entry_to_decision: dict[int, int] = {}  # entry_order_id -> decision_id
        self._flatten_orders: dict[int, int] = {}  # flatten_order_id -> position_id
        self._flattening: set[int] = set()  # position_ids with a flatten in flight

    # --- lifecycle -----------------------------------------------------------

    def run_forever(self) -> None:
        self.bootstrap()
        LOGGER.info("TradeExecutor runtime started (mode=%s)", self._settings.trading_mode)
        last_heartbeat = 0.0
        while True:
            try:
                processed = self.tick()
                now = self._monotonic()
                if processed == 0 and now - last_heartbeat >= self._settings.heartbeat_interval_seconds:
                    self._log_heartbeat()
                    last_heartbeat = now
            except Exception:
                LOGGER.exception("Top-level TradeExecutor cycle failure")
                time.sleep(max(1, self._settings.poll_interval_seconds))

    def bootstrap(self) -> None:
        self._redis.ping()
        self._redis.ensure_streams_and_group()
        try:
            self._broker.connect()
        except Exception:
            LOGGER.exception("Initial IBKR connect failed; will retry with backoff")
        self.reconcile_on_startup()

    def tick(self) -> int:
        """One loop iteration. Returns the number of signals processed."""
        self._apply_broker_events()
        self._evaluate_time_exits(self._clock())
        connected = self._reconnect_if_needed()
        if not connected:
            # Fail closed: pause new entries while disconnected.
            return 0
        self._check_fill_timeouts()
        return self.run_once()

    def run_once(self) -> int:
        messages = self._redis.read(
            count=max(1, self._settings.batch_size),
            block_ms=max(1, self._settings.block_ms),
        )
        for message in messages:
            self._process_with_retry(message)
        return len(messages)

    def _reconnect_if_needed(self) -> bool:
        if self._broker.is_connected():
            return True
        try:
            self._broker.connect()
        except Exception:
            LOGGER.warning("IBKR reconnect attempt failed")
        return self._broker.is_connected()

    def _process_with_retry(self, message: SignalMessage) -> None:
        try:
            self.process_message(message)
        except Exception as exc:
            delivery_count = self._redis.delivery_count(message.message_id)
            error_code = exc.__class__.__name__
            if delivery_count >= self._settings.max_delivery_attempts:
                LOGGER.exception(
                    "TradeExecutor message failed permanently id=%s delivery_count=%s",
                    message.message_id, delivery_count,
                )
                self._redis.publish_dlq(message=message, error_code=error_code)
                self._redis.ack(message.message_id)
            else:
                LOGGER.exception(
                    "TradeExecutor message failed, remains pending id=%s delivery_count=%s",
                    message.message_id, delivery_count,
                )

    # --- signal processing ---------------------------------------------------

    def process_message(self, message: SignalMessage) -> None:
        if not message.is_thesis_card:
            self._redis.ack(message.message_id)
            return
        card = message.as_thesis_card()
        if card is None:
            self._redis.publish_dlq(message=message, error_code="malformed_thesis_card")
            self._redis.ack(message.message_id)
            return

        now = self._clock()
        gate = evaluate_admission_gate(
            card=card,
            now=now,
            min_confidence=self._settings.min_confidence,
            in_watchlist=self._is_in_watchlist(card),
            review_state=self._review_state(card),
            has_open_or_working_position=self._repository.has_open_or_working_for_instrument(
                ticker=card.ticker, exchange_code=card.exchange_code
            ),
            horizon_map=self._settings.time_horizon_days_map,
        )
        if not gate.passed:
            self._persist_and_ack(card, gate, message, now)
            return

        # Price discovery (fresh IBKR quote; fail closed on staleness).
        quote = self._broker.snapshot_quote(
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            timeout_seconds=float(self._settings.quote_max_age_seconds),
        )
        if (
            quote is None
            or not quote.has_two_sided
            or not quote.is_fresh(now=now, max_age_seconds=self._settings.quote_max_age_seconds)
        ):
            self._persist_and_ack(
                card, GateOutcome.reject(DecisionReason.QUOTE_UNAVAILABLE), message, now
            )
            return

        context = self._market_context_for(card)
        atr = getattr(context, "atr_20d", None) if context is not None else None
        if atr is None or atr <= 0:
            self._persist_and_ack(
                card, GateOutcome.reject(DecisionReason.ATR_UNAVAILABLE), message, now
            )
            return

        entry = entry_limit_price(
            direction=card.direction,
            bid=quote.bid,
            ask=quote.ask,
            slippage_bps=self._settings.entry_limit_slippage_bps,
        )
        levels = construct_levels(
            direction=card.direction,
            entry=entry,
            atr_20d=atr,
            atr_stop_mult=self._settings.atr_stop_mult,
            take_profit_r=self._settings.take_profit_r,
        )

        count, deployed = self._repository.portfolio_totals()
        headroom = self._settings.max_portfolio_exposure - deployed
        order = size_position(
            max_loss_usd=card.max_loss_usd,
            entry=entry,
            stop=levels.stop,
            max_position_size=self._settings.max_position_size,
            portfolio_headroom=headroom,
        )
        if order.quantity < 1:
            self._persist_and_ack(
                card, GateOutcome.reject(DecisionReason.SIZE_BELOW_ONE_SHARE), message, now,
                entry=entry, levels=levels, atr=atr,
            )
            return

        trade_date = self._trading_day(now)
        daily = self._repository.get_or_create_daily_risk(trade_date)
        sector = self._sector_for(card)
        risk = evaluate_risk_gate(
            new_quantity=order.quantity,
            entry=entry,
            portfolio=PortfolioState(
                open_and_working_count=count,
                deployed_capital=deployed,
                sector_exposure=self._sector_exposure(sector),
                sector_known=sector is not None,
            ),
            daily=DailyRiskState(
                realized_pnl=daily.realized_pnl,
                unrealized_pnl=self._unrealized_pnl(),
                trades_count=daily.trades_count,
                halted=daily.halted,
            ),
            max_positions=self._settings.max_positions,
            max_portfolio_exposure=self._settings.max_portfolio_exposure,
            max_sector_exposure=self._settings.max_sector_exposure,
            daily_loss_limit=self._settings.daily_loss_limit,
            max_daily_trades=self._settings.max_daily_trades,
        )
        if risk.details.get("halt_triggered"):
            self._repository.set_halted(trade_date)

        decision_id = self._persist_and_ack(
            card, risk, message, now, entry=entry, levels=levels, atr=atr,
            quantity=order.quantity, ack=risk.passed is False,
        )
        if not risk.passed or decision_id is None:
            return  # rejected, duplicate, or already acked by _persist_and_ack

        self._submit_bracket(card, decision_id, order.quantity, entry, levels, trade_date, message)

    def _submit_bracket(self, card, decision_id, quantity, entry, levels, trade_date, message) -> None:
        oca_group = f"te-{decision_id}"
        request = BracketRequest(
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            side=card.direction,
            quantity=quantity,
            entry_limit_price=entry,
            stop_price=levels.stop,
            take_profit_price=levels.target,
            oca_group=oca_group,
            outside_rth=self._settings.outside_rth,
        )
        try:
            handle = self._broker.submit_bracket(request)
        except Exception:
            LOGGER.exception("Bracket submission failed decision_id=%s", decision_id)
            self._repository.insert_execution_leg(
                decision_id=decision_id, leg_role=LegRole.ENTRY, ibkr_order_id=None,
                ibkr_oca_group=oca_group, status="rejected",
            )
            self._redis.ack(message.message_id)
            return

        for leg_role, order_id in (
            (LegRole.ENTRY, handle.entry_order_id),
            (LegRole.STOP, handle.stop_order_id),
            (LegRole.TAKE_PROFIT, handle.take_profit_order_id),
        ):
            self._repository.insert_execution_leg(
                decision_id=decision_id, leg_role=leg_role, ibkr_order_id=order_id,
                ibkr_oca_group=oca_group, status="submitted",
            )
        self._repository.increment_trades_count(trade_date)
        self._brackets[decision_id] = _BracketRef(
            decision_id=decision_id, card=card, side=card.direction, quantity=quantity,
            stop_price=levels.stop, take_profit_price=levels.target,
            entry_order_id=handle.entry_order_id, stop_order_id=handle.stop_order_id,
            take_profit_order_id=handle.take_profit_order_id, entry_limit_price=entry,
            submitted_at=self._monotonic(),
        )
        self._entry_to_decision[handle.entry_order_id] = decision_id
        LOGGER.info(
            "Submitted bracket decision_id=%s %s %s x%s entry=%.4f stop=%.4f target=%.4f",
            decision_id, card.direction, card.ticker, quantity, entry, levels.stop, levels.target,
        )
        self._redis.ack(message.message_id)

    def _persist_and_ack(
        self, card: ThesisCard, outcome: GateOutcome, message: SignalMessage, now: datetime,
        *, entry: float | None = None, levels=None, atr: float | None = None,
        quantity: int | None = None, ack: bool = True,
    ) -> int | None:
        """Persist exactly one decision row; ack when the outcome is terminal here.

        Returns the new decision id (or None on a duplicate-card UNIQUE violation).
        When ``ack`` is True the message is acknowledged; a passed decision that
        still needs order submission passes ``ack=False`` so submission acks it.
        """
        record = DecisionRecord(
            id=None,
            thesis_card_id=card.thesis_card_id,
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            action=card.direction,
            quantity=quantity,
            order_type="limit" if outcome.passed else None,
            limit_price=entry if outcome.passed else None,
            entry_price=entry,
            stop_price=getattr(levels, "stop", None),
            take_profit_price=getattr(levels, "target", None),
            atr_20d=atr,
            risk_amount_usd=card.max_loss_usd,
            confidence=card.confidence,
            signal_strength=None,
            source_analysis_ids=card.source_analysis_ids,
            risk_check_passed=outcome.passed,
            risk_check_details=str(outcome.reason),
            decided_at=now,
        )
        decision_id = self._repository.insert_decision(record)
        if decision_id is None:
            # UNIQUE(thesis_card_id) violation: the card was already acted upon.
            LOGGER.info("Duplicate card acked card_id=%s", card.thesis_card_id)
            self._redis.ack(message.message_id)
            return None
        if not outcome.passed:
            LOGGER.info(
                "Decision recorded card_id=%s reason=%s", card.thesis_card_id, outcome.reason
            )
            self._redis.ack(message.message_id)
            return decision_id
        if ack:
            self._redis.ack(message.message_id)
        return decision_id

    # --- broker event application --------------------------------------------

    def _apply_broker_events(self) -> None:
        try:
            events = self._broker.drain_events()
        except Exception:
            LOGGER.exception("Failed to drain broker events")
            return
        for event in events:
            try:
                self._apply_event(event)
            except Exception:
                LOGGER.exception("Failed to apply broker event %s", event)

    def _apply_event(self, event: BrokerEvent) -> None:
        if event.kind in (BrokerEventKind.CONNECTED, BrokerEventKind.DISCONNECTED):
            LOGGER.info("Broker %s", event.kind)
            return
        if event.kind == BrokerEventKind.COMMISSION_REPORT:
            if event.ibkr_order_id is not None and event.commission is not None:
                self._repository.update_execution(
                    ibkr_order_id=event.ibkr_order_id, commission=event.commission
                )
            return
        # ORDER_STATUS / EXEC_DETAIL: treat any positive fill as a fill event.
        order_id = event.ibkr_order_id
        if order_id is None:
            return
        execution = self._repository.get_execution_by_order_id(order_id)
        if execution is None:
            # Could be a flatten order closing a position on a time-exit.
            if order_id in self._flatten_orders and (event.filled_qty or 0) > 0:
                self._close_flattened_position(order_id, event)
            return

        status = (event.status or "").lower()
        filled = event.filled_qty or 0
        if status == "cancelled":
            self._repository.update_execution(ibkr_order_id=order_id, status="cancelled")
            return
        if filled <= 0:
            return

        if execution.leg_role == LegRole.ENTRY:
            self._on_entry_fill(execution.decision_id, order_id, event)
        elif execution.leg_role in (LegRole.STOP, LegRole.TAKE_PROFIT):
            self._on_exit_fill(execution.decision_id, execution.leg_role, order_id, event)

    def _on_entry_fill(self, decision_id: int, order_id: int, event: BrokerEvent) -> None:
        bracket = self._brackets.get(decision_id)
        card = bracket.card if bracket else None
        filled = int(event.filled_qty or 0)
        avg = float(event.avg_fill_price or 0.0)
        target_qty = bracket.quantity if bracket else filled
        leg_status = "filled" if filled >= target_qty else "partial"
        self._repository.update_execution(
            ibkr_order_id=order_id, status=leg_status, fill_price=avg, fill_quantity=filled,
            executed_at=event.ts,
        )
        existing = self._repository.get_open_position_by_decision(decision_id)
        if existing is None:
            time_exit_at = self._compute_time_exit(card, event.ts)
            side = PositionSide.LONG if (bracket and bracket.side == TradeDirection.BUY) else PositionSide.SHORT
            self._repository.open_position(
                thesis_card_id=(card.thesis_card_id if card else ""),
                decision_id=decision_id,
                ticker=(card.ticker if card else event.ticker or ""),
                exchange_code=(card.exchange_code if card else event.exchange_code or ""),
                side=side,
                quantity=filled,
                avg_entry_price=avg,
                stop_price=(bracket.stop_price if bracket else 0.0),
                take_profit_price=(bracket.take_profit_price if bracket else 0.0),
                time_exit_at=time_exit_at,
                opened_at=event.ts,
            )
        else:
            self._repository.update_position(
                position_id=existing.id, quantity=filled, avg_entry_price=avg
            )

    def _on_exit_fill(self, decision_id: int, leg_role: str, order_id: int, event: BrokerEvent) -> None:
        position = self._repository.get_open_position_by_decision(decision_id)
        self._repository.update_execution(
            ibkr_order_id=order_id, status="filled",
            fill_price=event.avg_fill_price, fill_quantity=event.filled_qty,
            executed_at=event.ts,
        )
        if position is None:
            return
        exit_reason = ExitReason.STOP if leg_role == LegRole.STOP else ExitReason.TAKE_PROFIT
        realized = self._realized_pnl(position, float(event.avg_fill_price or 0.0), event.realized_pnl)
        self._repository.close_position(
            position_id=position.id, exit_reason=exit_reason,
            realized_pnl=realized, closed_at=event.ts,
        )
        self._repository.add_realized_pnl(self._trading_day(event.ts), realized)
        self._cancel_sibling(decision_id, leg_role)
        self._brackets.pop(decision_id, None)

    def _cancel_sibling(self, decision_id: int, filled_leg: str) -> None:
        bracket = self._brackets.get(decision_id)
        if bracket is None:
            return
        sibling_id = (
            bracket.take_profit_order_id if filled_leg == LegRole.STOP else bracket.stop_order_id
        )
        try:
            self._broker.cancel_order(sibling_id)
            self._repository.update_execution(ibkr_order_id=sibling_id, status="cancelled")
        except Exception:
            LOGGER.exception("Failed to cancel sibling leg order_id=%s", sibling_id)

    def _close_flattened_position(self, order_id: int, event: BrokerEvent) -> None:
        position_id = self._flatten_orders.pop(order_id, None)
        if position_id is None:
            return
        position = self._repository.get_position_by_id(position_id)
        if position is None or position.closed_at is not None:
            return
        realized = self._realized_pnl(position, float(event.avg_fill_price or 0.0), event.realized_pnl)
        self._repository.close_position(
            position_id=position_id, exit_reason=ExitReason.TIME,
            realized_pnl=realized, closed_at=event.ts,
        )
        self._repository.add_realized_pnl(self._trading_day(event.ts), realized)
        self._flattening.discard(position_id)
        self._brackets.pop(position.decision_id, None)

    # --- fill timeout (re-price once) ---------------------------------------

    def _check_fill_timeouts(self) -> None:
        timeout = self._settings.order_fill_timeout_seconds
        now = self._monotonic()
        for decision_id, bracket in list(self._brackets.items()):
            # Only entries still fully unfilled are candidates (position not opened).
            if self._repository.get_open_position_by_decision(decision_id) is not None:
                continue
            if now - bracket.submitted_at < timeout:
                continue
            if not bracket.repriced:
                self._reprice_entry(bracket)
            else:
                self._abandon_entry(bracket)

    def _reprice_entry(self, bracket: _BracketRef) -> None:
        quote = self._broker.snapshot_quote(
            ticker=bracket.card.ticker, exchange_code=bracket.card.exchange_code,
            timeout_seconds=float(self._settings.quote_max_age_seconds),
        )
        if quote is None or not quote.has_two_sided:
            return  # retry next tick
        new_price = entry_limit_price(
            direction=bracket.side, bid=quote.bid, ask=quote.ask,
            slippage_bps=self._settings.entry_limit_slippage_bps,
        )
        try:
            self._broker.replace_order_price(bracket.entry_order_id, new_price)
        except Exception:
            LOGGER.exception("Failed to re-price entry order_id=%s", bracket.entry_order_id)
            return
        bracket.repriced = True
        bracket.submitted_at = self._monotonic()
        LOGGER.info("Re-priced entry decision_id=%s new_limit=%.4f", bracket.decision_id, new_price)

    def _abandon_entry(self, bracket: _BracketRef) -> None:
        for order_id in (bracket.entry_order_id, bracket.stop_order_id, bracket.take_profit_order_id):
            try:
                self._broker.cancel_order(order_id)
                self._repository.update_execution(ibkr_order_id=order_id, status="cancelled")
            except Exception:
                LOGGER.exception("Failed to cancel order_id=%s while abandoning", order_id)
        self._brackets.pop(bracket.decision_id, None)
        LOGGER.info("Abandoned unfilled entry decision_id=%s", bracket.decision_id)

    # --- time exits ----------------------------------------------------------

    def _evaluate_time_exits(self, now: datetime) -> None:
        try:
            positions = self._repository.list_open_positions_past_time_exit(now)
        except Exception:
            LOGGER.exception("Failed to load positions for time-exit")
            return
        for position in positions:
            if position.id in self._flattening:
                continue
            if not self._settings.outside_rth and not self._calendar.is_rth(now):
                continue  # defer to next regular session
            self._flatten_position(position)

    def _flatten_position(self, position) -> None:
        quote = self._broker.snapshot_quote(
            ticker=position.ticker, exchange_code=position.exchange_code,
            timeout_seconds=float(self._settings.quote_max_age_seconds),
        )
        if quote is None or not quote.has_two_sided:
            return  # cannot price the flatten; retry next tick
        closing_side = TradeDirection.SELL if position.side == PositionSide.LONG else TradeDirection.BUY
        limit_price = entry_limit_price(
            direction=closing_side, bid=quote.bid, ask=quote.ask,
            slippage_bps=self._settings.entry_limit_slippage_bps,
        )
        bracket = self._brackets.get(position.decision_id)
        if bracket is not None:
            for order_id in (bracket.stop_order_id, bracket.take_profit_order_id):
                try:
                    self._broker.cancel_order(order_id)
                    self._repository.update_execution(ibkr_order_id=order_id, status="cancelled")
                except Exception:
                    LOGGER.exception("Failed to cancel residual leg order_id=%s", order_id)
        try:
            handle = self._broker.submit_flatten(
                FlattenRequest(
                    ticker=position.ticker, exchange_code=position.exchange_code,
                    side_to_close=closing_side, quantity=position.quantity,
                    limit_price=limit_price, outside_rth=self._settings.outside_rth,
                )
            )
        except Exception:
            LOGGER.exception("Failed to submit flatten position_id=%s", position.id)
            return
        self._flatten_orders[handle.entry_order_id] = position.id
        self._flattening.add(position.id)
        LOGGER.info("Time-exit flatten submitted position_id=%s", position.id)

    # --- startup reconciliation ---------------------------------------------

    def reconcile_on_startup(self) -> None:
        """Close out orphaned passed decisions; never auto-resubmit (fail closed)."""
        try:
            orphans = self._repository.list_passed_decisions_without_executions()
        except Exception:
            LOGGER.exception("Reconciliation query failed")
            return
        broker_orders = []
        try:
            broker_orders = self._broker.list_open_orders()
        except Exception:
            LOGGER.warning("Could not list broker open orders during reconciliation")
        matched = {(o.ticker, o.exchange_code) for o in broker_orders}
        for decision in orphans:
            key = (decision.ticker, decision.exchange_code)
            if key in matched:
                # A broker order exists for this instrument; leave it for callbacks.
                continue
            self._repository.mark_decision_orphaned(decision.id)
            LOGGER.warning(
                "Closed orphaned decision id=%s card_id=%s (never resubmitted)",
                decision.id, decision.thesis_card_id,
            )

    # --- helpers -------------------------------------------------------------

    def status(self) -> TradeExecutorRuntimeStatus:
        try:
            open_positions = len(self._repository.list_open_positions())
        except Exception:
            open_positions = 0
        return TradeExecutorRuntimeStatus(
            signal_length=self._redis.stream_length(),
            pending_count=self._redis.pending_count(),
            open_positions=open_positions,
            connected=self._safe_is_connected(),
        )

    def _safe_is_connected(self) -> bool:
        try:
            return self._broker.is_connected()
        except Exception:
            return False

    def _log_heartbeat(self) -> None:
        status = self.status()
        LOGGER.info(
            "TradeExecutor heartbeat signal_length=%s pending=%s open_positions=%s connected=%s",
            status.signal_length, status.pending_count, status.open_positions, status.connected,
        )

    def _is_in_watchlist(self, card: ThesisCard) -> bool:
        if self._watchlist_reader is None:
            return False
        record = self._watchlist_reader.get_watchlist_record(
            ticker=card.ticker, exchange_code=card.exchange_code
        )
        return record is not None and getattr(record, "is_active", False)

    def _review_state(self, card: ThesisCard) -> str | None:
        if self._review_reader is None:
            return None
        return self._review_reader.get_review_state(card_id=card.thesis_card_id)

    def _market_context_for(self, card: ThesisCard):
        if self._market_context is None:
            return None
        return self._market_context.get_market_context(
            ticker=card.ticker, exchange_code=card.exchange_code, refresh_if_stale=True
        )

    def _sector_for(self, card: ThesisCard) -> str | None:
        if self._sector_reader is None:
            return None
        return self._sector_reader.get_sector(ticker=card.ticker, exchange_code=card.exchange_code)

    def _sector_exposure(self, sector: str | None) -> float:
        if sector is None or self._sector_reader is None:
            return 0.0
        total = 0.0
        for entry in self._repository.list_exposure_entries():
            entry_sector = self._sector_reader.get_sector(
                ticker=entry.ticker, exchange_code=entry.exchange_code
            )
            if entry_sector == sector:
                total += entry.notional
        return total

    def _unrealized_pnl(self) -> float:
        try:
            return self._broker.account_snapshot().total_unrealized_pnl
        except Exception:
            return 0.0

    def _compute_time_exit(self, card: ThesisCard | None, fill_time: datetime) -> datetime | None:
        if card is None:
            return None
        days = self._settings.time_horizon_days_map.get(card.time_horizon)
        if days is None:
            return None
        return self._calendar.time_exit_at(fill_time=fill_time, trading_days=days)

    def _realized_pnl(self, position, exit_price: float, event_pnl: float | None) -> float:
        if event_pnl is not None:
            return event_pnl
        avg = position.avg_entry_price or 0.0
        if position.side == PositionSide.LONG:
            return (exit_price - avg) * position.quantity
        return (avg - exit_price) * position.quantity

    def _trading_day(self, now: datetime) -> date:
        return now.astimezone(self._tz).date()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
