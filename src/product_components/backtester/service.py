from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from .clients import BarsProvider, CardsProvider
from .engine import BacktesterEngine
from .models import BacktestRunParams
from .repository import BacktesterRepository, dataset_snapshot_hash
from .settings import BacktesterSettings

LOGGER = logging.getLogger("backtester")

# Reports run progress to an external observer: (phase, done, total, ticker) where phase is
# "prewarming" | "simulating".
ProgressSink = Callable[[str, int, int, "str | None"], None]


def new_run_id() -> str:
    return f"bt_{uuid.uuid4().hex}"


class BacktesterService:
    def __init__(
        self,
        *,
        settings: BacktesterSettings,
        repository: BacktesterRepository,
        cards_provider: CardsProvider,
        bars_provider: BarsProvider,
        progress: ProgressSink | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._cards = cards_provider
        self._bars = bars_provider
        self._progress = progress

    def run(self, params: BacktestRunParams) -> None:
        self._validate_params(params)
        cards = self._cards.export_cards(
            window_start_at=params.window_start_at,
            window_end_at=params.window_end_at,
        )
        snapshot_hash = dataset_snapshot_hash(cards)
        self._repository.create_run(params=params, dataset_snapshot_hash=snapshot_hash)

        self._prefetch_market_data(cards, params)

        self._report_progress("simulating", 0, 0, None)
        try:
            result = BacktesterEngine(
                params=params,
                cards_provider=self._cards,
                bars_provider=self._bars,
            ).run()

            if self._settings.persist_card_snapshots:
                self._repository.insert_card_snapshots(
                    run_id=params.run_id, snapshots=result.card_snapshots
                )
            self._repository.insert_trades(run_id=params.run_id, trades=result.trades)
            if self._settings.persist_equity_points:
                self._repository.insert_equity_points(
                    run_id=params.run_id, points=result.equity_points
                )
            self._repository.finalize_run_success(
                run_id=params.run_id, metrics=result.metrics
            )
        except Exception as error:
            self._repository.finalize_run_failure(
                run_id=params.run_id,
                error_code=error.__class__.__name__,
                details={"message": str(error)[:1000]},
            )
            raise

    def _prefetch_market_data(self, cards: list, params: BacktestRunParams) -> None:
        """Warm the DB with bars for every instrument before the deterministic sim runs.

        Decouples the slow, rate-limited network backfill from the engine, which then reads
        bars purely from the DB. No-ops if the bars provider does not support warming.
        """
        warm = getattr(self._bars, "warm", None)
        if warm is None:
            return
        instruments = sorted(
            {(card.ticker, card.exchange_code) for card in cards}
        )
        if not instruments:
            return
        interval = params.execution_model.bar_interval
        LOGGER.info(
            "prefetching market data run_id=%s instruments=%d interval=%s",
            params.run_id,
            len(instruments),
            interval,
        )

        total = len(instruments)
        self._report_progress("prewarming", 0, total, None)

        def _progress(done: int, total: int, ticker: str, status: str) -> None:
            LOGGER.info("market data %s %d/%d %s", status, done, total, ticker)
            self._report_progress("prewarming", done, total, ticker)

        warm(
            instruments,
            interval=interval,
            start=params.window_start_at,
            end=params.window_end_at,
            progress=_progress,
        )
        LOGGER.info("prefetch complete run_id=%s", params.run_id)

    def _report_progress(self, phase: str, done: int, total: int, ticker: str | None) -> None:
        if self._progress is not None:
            self._progress(phase, done, total, ticker)

    def _validate_params(self, params: BacktestRunParams) -> None:
        if params.window_start_at >= params.window_end_at:
            raise ValueError("invalid_window")
        if params.ideal_fetch_delay_seconds < 0 or params.ideal_thesis_delay_seconds < 0:
            raise ValueError("invalid_ideal_delays")
