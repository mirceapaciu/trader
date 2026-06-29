from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.product_components.backtester.models import BacktestRunParams
from src.product_components.backtester.service import BacktesterService

_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
_END = datetime(2026, 6, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Card:
    ticker: str
    exchange_code: str


class _FakeBars:
    """Mimics MarketDataBarsProvider.warm by reporting one fetch per instrument."""

    def historical_bars(self, *, ticker, exchange_code, interval, start, end):  # pragma: no cover
        return []

    def warm(self, instruments, *, interval, start, end, progress=None):
        instruments = list(instruments)
        for index, (ticker, _exchange) in enumerate(instruments, start=1):
            if progress is not None:
                progress(index, len(instruments), ticker, "fetched")


def _params() -> BacktestRunParams:
    return BacktestRunParams(run_id="bt_1", window_start_at=_START, window_end_at=_END)


def _service(progress) -> BacktesterService:
    return BacktesterService(
        settings=None,  # type: ignore[arg-type]
        repository=None,  # type: ignore[arg-type]
        cards_provider=None,  # type: ignore[arg-type]
        bars_provider=_FakeBars(),
        progress=progress,
    )


def test_prefetch_forwards_prewarming_progress_to_sink() -> None:
    events: list[tuple[str, int, int, str | None]] = []
    service = _service(lambda phase, done, total, ticker: events.append((phase, done, total, ticker)))

    cards = [_Card("AAPL", "XNAS"), _Card("MSFT", "XNAS")]
    service._prefetch_market_data(cards, _params())

    # An initial 0/total tick, then one tick per instrument, all in the prewarming phase.
    assert events[0] == ("prewarming", 0, 2, None)
    assert ("prewarming", 1, 2, "AAPL") in events
    assert ("prewarming", 2, 2, "MSFT") in events
    assert {e[0] for e in events} == {"prewarming"}


def test_prefetch_without_sink_is_noop() -> None:
    service = _service(None)
    # No progress sink and no instruments: must not raise.
    service._prefetch_market_data([], _params())
