"""Time-ordered OHLCV bar series for the backtest simulation harness."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar covering the interval beginning at ``start_at``."""

    start_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def _as_utc(ts: datetime) -> datetime:
    """Treat a naive datetime as UTC; otherwise convert to UTC."""

    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class BarSeries:
    """An immutable view over bars sorted ascending by ``start_at``.

    Duplicate ``start_at`` values keep the last bar provided. All query
    methods accept timezone-aware UTC datetimes; naive datetimes are treated
    as UTC.
    """

    def __init__(self, bars: Iterable[Bar]) -> None:
        deduped: dict[datetime, Bar] = {}
        for bar in bars:
            deduped[_as_utc(bar.start_at)] = bar
        ordered = sorted(deduped.items(), key=lambda item: item[0])
        self._bars: list[Bar] = [bar for _, bar in ordered]
        self._keys: list[datetime] = [key for key, _ in ordered]

    def first_at_or_after(self, ts: datetime) -> Bar | None:
        """Return the earliest bar with ``start_at >= ts``, or ``None``."""

        index = bisect.bisect_left(self._keys, _as_utc(ts))
        if index >= len(self._bars):
            return None
        return self._bars[index]

    def iter_from(self, ts: datetime) -> Iterator[Bar]:
        """Yield bars with ``start_at >= ts`` in ascending order."""

        index = bisect.bisect_left(self._keys, _as_utc(ts))
        yield from self._bars[index:]

    def iter_after(self, ts: datetime) -> Iterator[Bar]:
        """Yield bars with ``start_at > ts`` in ascending order."""

        index = bisect.bisect_right(self._keys, _as_utc(ts))
        yield from self._bars[index:]

    def last(self) -> Bar | None:
        """Return the latest bar, or ``None`` if the series is empty."""

        if not self._bars:
            return None
        return self._bars[-1]

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def is_empty(self) -> bool:
        return not self._bars
