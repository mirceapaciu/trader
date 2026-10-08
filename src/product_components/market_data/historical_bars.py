from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class HistoricalBarsFetchIncomplete(RuntimeError):
    """A provider stopped before confirming the complete requested range.

    Providers may attach bars from completed pages so MarketData can retain useful
    work.  Callers must not advance the coverage ledger for this exception.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_bars: Iterable[Any] = (),
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_bars = list(partial_bars)
        self.cause = cause
