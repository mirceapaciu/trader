"""Trading-calendar helpers behind a small Protocol.

The default implementation uses the ``exchange-calendars`` dependency (NYSE /
``XNYS``); service tests inject a simple fake so they need neither the package
nor a specific market date. Kept in its own module so the calendar import is
isolated from the pure/service logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class TradingCalendar(Protocol):
    def time_exit_at(self, *, fill_time: datetime, trading_days: int) -> datetime:
        """Close time of the Nth trading session on/after ``fill_time``."""

    def is_rth(self, now: datetime) -> bool:
        """Whether the market is open (regular trading hours) at ``now``."""

    def next_session_open(self, now: datetime) -> datetime:
        """Open time of the next regular session at/after ``now``."""


class ExchangeCalendarsTradingCalendar:
    """NYSE-backed calendar using the ``exchange-calendars`` package."""

    def __init__(self, calendar_name: str = "XNYS") -> None:
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar(calendar_name)

    def time_exit_at(self, *, fill_time: datetime, trading_days: int) -> datetime:
        cal = self._cal
        ts = _to_utc(fill_time)
        # Session on or after the fill, then advance ``trading_days`` sessions.
        session = cal.minute_to_session(ts, direction="next")
        for _ in range(max(0, trading_days)):
            session = cal.next_session(session)
        close = cal.session_close(session)
        return _to_utc(close.to_pydatetime())

    def is_rth(self, now: datetime) -> bool:
        return bool(self._cal.is_open_on_minute(_to_utc(now)))

    def next_session_open(self, now: datetime) -> datetime:
        open_ts = self._cal.next_open(_to_utc(now))
        return _to_utc(open_ts.to_pydatetime())


class NaiveTradingCalendar:
    """Fallback calendar: calendar days, always-RTH. Used if the package is absent."""

    def time_exit_at(self, *, fill_time: datetime, trading_days: int) -> datetime:
        return _to_utc(fill_time) + timedelta(days=max(0, trading_days))

    def is_rth(self, now: datetime) -> bool:
        return True

    def next_session_open(self, now: datetime) -> datetime:
        return _to_utc(now)


def build_default_calendar() -> TradingCalendar:
    try:
        return ExchangeCalendarsTradingCalendar()
    except Exception:  # pragma: no cover - exercised only when the package is missing
        return NaiveTradingCalendar()


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
