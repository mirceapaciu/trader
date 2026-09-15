from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.product_components.backtester.models import BacktestRunParams
from src.product_components.backtester.service import BacktesterService, MarketDataUnavailableError
from src.product_components.market_data.models import (
    HistoricalBarsPrefetchOutcome,
    MarketDataProvider,
)

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


def _outcome(
    ticker: str,
    exchange_code: str,
    *,
    status: str,
    provider: MarketDataProvider | None = None,
    failure_category: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> HistoricalBarsPrefetchOutcome:
    return HistoricalBarsPrefetchOutcome(
        ticker=ticker,
        exchange_code=exchange_code,
        status=status,
        provider=provider,
        failure_category=failure_category,
        considered_providers=(MarketDataProvider.POLYGON,),
        error_code=error_code,
        error_message=error_message,
    )


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
            return {
                ("AAPL", "XNAS"): _outcome(
                    "AAPL",
                    "XNAS",
                    status="unavailable",
                    provider=MarketDataProvider.POLYGON,
                    failure_category="provider_error",
                    error_code="TimeoutError",
                    error_message="gateway timed out",
                )
            }

    service = BacktesterService(
        settings=None,  # type: ignore[arg-type]
        repository=None,  # type: ignore[arg-type]
        cards_provider=None,  # type: ignore[arg-type]
        bars_provider=_UnavailableBars(),
    )

    with pytest.raises(MarketDataUnavailableError, match="market_data_unavailable") as error:
        service._prefetch_market_data([_Card("AAPL", "XNAS")], _params())

    assert error.value.details["unavailable_instruments"] == [
        {
            "ticker": "AAPL",
            "exchange_code": "XNAS",
            "status": "unavailable",
            "provider": "polygon",
            "failure_category": "provider_error",
            "considered_providers": ["polygon"],
            "error_code": "TimeoutError",
            "error_message": "gateway timed out",
        }
    ]


def test_prefetch_allows_an_isolated_unavailable_instrument(caplog) -> None:
    class _PartiallyUnavailableBars(_FakeBars):
        def warm(self, instruments, *, interval, start, end, progress=None):
            return {
                ("AAPL", "XNAS"): _outcome("AAPL", "XNAS", status="fetched"),
                ("005930", "KRX"): _outcome(
                    "005930",
                    "KRX",
                    status="unavailable",
                    failure_category="no_provider_configured",
                ),
            }

    service = BacktesterService(
        settings=None,  # type: ignore[arg-type]
        repository=None,  # type: ignore[arg-type]
        cards_provider=None,  # type: ignore[arg-type]
        bars_provider=_PartiallyUnavailableBars(),
    )

    service._prefetch_market_data(
        [_Card("AAPL", "XNAS"), _Card("005930", "KRX")], _params()
    )

    assert "005930/KRX (no_provider_configured)" in caplog.text


def test_replay_persists_market_data_failure_before_simulation() -> None:
    class _UnavailableBars(_FakeBars):
        def warm(self, instruments, *, interval, start, end, progress=None):
            return {
                ("AAPL", "XNAS"): _outcome(
                    "AAPL",
                    "XNAS",
                    status="unavailable",
                    provider=MarketDataProvider.POLYGON,
                    failure_category="provider_error",
                    error_code="TimeoutError",
                    error_message="gateway timed out",
                )
            }

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
            "message": "No usable historical market data was available from the configured sources.",
            "interval": "1m",
            "unavailable_instruments": [
                {
                    "ticker": "AAPL",
                    "exchange_code": "XNAS",
                    "status": "unavailable",
                    "provider": "polygon",
                    "failure_category": "provider_error",
                    "considered_providers": ["polygon"],
                    "error_code": "TimeoutError",
                    "error_message": "gateway timed out",
                }
            ],
        },
    }
