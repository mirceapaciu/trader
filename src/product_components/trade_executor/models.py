from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class TradeDirection(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class LegRole(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    TAKE_PROFIT = "take_profit"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class ExecStatus(StrEnum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ExitReason(StrEnum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    TIME = "time"
    MANUAL = "manual"
    INVALIDATION = "invalidation"


class DecisionReason(StrEnum):
    """Machine-readable reason codes persisted in t_trade_decisions.risk_check_details."""

    # Admission-gate drops.
    DIRECTION_HOLD = "direction_hold"
    CARD_EXPIRED = "card_expired"
    BELOW_MIN_CONFIDENCE = "below_min_confidence"
    NOT_IN_WATCHLIST = "not_in_watchlist"
    POSITION_EXISTS = "position_exists"
    DUPLICATE_CARD = "duplicate_card"
    REVIEW_NOT_APPROVED = "review_not_approved"
    HORIZON_UNMAPPED = "horizon_unmapped"
    # Risk-gate / price-discovery rejections.
    QUOTE_UNAVAILABLE = "quote_unavailable"
    ATR_UNAVAILABLE = "atr_unavailable"
    SIZE_BELOW_ONE_SHARE = "size_below_one_share"
    PORTFOLIO_CAP_EXCEEDED = "portfolio_cap_exceeded"
    DAILY_LOSS_HALT = "daily_loss_halt"
    MAX_DAILY_TRADES_REACHED = "max_daily_trades_reached"
    BROKER_REJECTED = "broker_rejected"
    # Reconciliation.
    DECISION_ORPHANED = "decision_orphaned"
    # Sentinel: admitted and risk-approved.
    ADMITTED = "admitted"


@dataclass(frozen=True)
class ThesisCard:
    thesis_card_id: str
    ticker: str
    exchange_code: str
    direction: str
    time_horizon: str
    strategy: str
    confidence: float
    max_loss_usd: float
    stop_condition: str | None
    invalidation_condition: str | None
    source_analysis_ids: list[int]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class SignalMessage:
    """A decoded signal_queue envelope."""

    message_id: str
    event_id: str
    event_type: str
    dedupe_key: str
    payload: dict[str, Any]
    raw_fields: dict[str, str] = field(default_factory=dict)

    @property
    def is_thesis_card(self) -> bool:
        return self.event_type == "thesis_card.created"

    def as_thesis_card(self) -> ThesisCard | None:
        payload = self.payload
        thesis_card_id = payload.get("thesis_card_id")
        ticker = payload.get("ticker")
        exchange_code = payload.get("exchange_code")
        direction = payload.get("direction")
        time_horizon = payload.get("time_horizon")
        created_at = _parse_datetime(payload.get("created_at"))
        expires_at = _parse_datetime(payload.get("expires_at"))
        confidence = _parse_float(payload.get("confidence"))
        risk_box = payload.get("risk_box") or {}
        max_loss_usd = _parse_float(risk_box.get("max_loss_usd")) if isinstance(risk_box, dict) else None

        if not (
            isinstance(thesis_card_id, str)
            and thesis_card_id
            and isinstance(ticker, str)
            and ticker
            and isinstance(exchange_code, str)
            and exchange_code
            and isinstance(direction, str)
            and isinstance(time_horizon, str)
            and created_at is not None
            and expires_at is not None
            and confidence is not None
            and max_loss_usd is not None
        ):
            return None

        source_ids_raw = payload.get("source_analysis_ids") or []
        source_analysis_ids = [int(x) for x in source_ids_raw if _is_intlike(x)]

        return ThesisCard(
            thesis_card_id=thesis_card_id,
            ticker=ticker.strip().upper(),
            exchange_code=exchange_code.strip().upper(),
            direction=direction.strip().lower(),
            time_horizon=time_horizon.strip(),
            strategy=str(payload.get("strategy") or ""),
            confidence=confidence,
            max_loss_usd=max_loss_usd,
            stop_condition=(str(risk_box.get("stop_condition")) if isinstance(risk_box, dict) and risk_box.get("stop_condition") is not None else None),
            invalidation_condition=(str(risk_box.get("invalidation_condition")) if isinstance(risk_box, dict) and risk_box.get("invalidation_condition") is not None else None),
            source_analysis_ids=source_analysis_ids,
            created_at=created_at,
            expires_at=expires_at,
        )


@dataclass(frozen=True)
class TradeLevels:
    entry: float
    stop: float
    target: float

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)


@dataclass(frozen=True)
class SizedOrder:
    quantity: int
    notional: float


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    reason: DecisionReason
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def admit(cls, **details: Any) -> "GateOutcome":
        return cls(passed=True, reason=DecisionReason.ADMITTED, details=details)

    @classmethod
    def reject(cls, reason: DecisionReason, **details: Any) -> "GateOutcome":
        return cls(passed=False, reason=reason, details=details)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _is_intlike(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False
