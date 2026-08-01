from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.product_components.backtester.models import BacktestRunParams
from src.product_components.backtester.service import BacktesterService, MarketDataUnavailableError

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


def test_prefetch_fails_when_market_data_provider_reports_unavailable() -> None:
    class _UnavailableBars(_FakeBars):
        def warm(self, instruments, *, interval, start, end, progress=None):
            return {("AAPL", "XNAS"): "unavailable"}

    service = BacktesterService(
        settings=None,  # type: ignore[arg-type]
        repository=None,  # type: ignore[arg-type]
        cards_provider=None,  # type: ignore[arg-type]
        bars_provider=_UnavailableBars(),
    )

    with pytest.raises(MarketDataUnavailableError, match="market_data_unavailable") as error:
        service._prefetch_market_data([_Card("AAPL", "XNAS")], _params())

    assert error.value.details["unavailable_instruments"] == [
        {"ticker": "AAPL", "exchange_code": "XNAS", "status": "unavailable"}
    ]


def test_replay_persists_market_data_failure_before_simulation() -> None:
    class _UnavailableBars(_FakeBars):
        def warm(self, instruments, *, interval, start, end, progress=None):
            return {("AAPL", "XNAS"): "unavailable"}

    class _Cards:
        def export_cards(self, **_kwargs):
            return [
                SimpleNamespace(
                    id="card-1",
                    ticker="AAPL",
                    exchange_code="XNAS",
                    created_at=_START,
                )
            ]

    class _Repository:
        failure: dict | None = None

        def create_run(self, **_kwargs):
            return None

        def finalize_run_failure(self, **kwargs):
            self.failure = kwargs

    repository = _Repository()
    service = BacktesterService(
        settings=None,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        cards_provider=_Cards(),
        bars_provider=_UnavailableBars(),
    )

    with pytest.raises(MarketDataUnavailableError):
        service.run(_params())

    assert repository.failure == {
        "run_id": "bt_1",
        "error_code": "MarketDataUnavailableError",
        "details": {
            "message": "Historical market data could not be received for the backtest.",
            "interval": "1m",
            "unavailable_instruments": [
                {"ticker": "AAPL", "exchange_code": "XNAS", "status": "unavailable"}
            ],
        },
    }
