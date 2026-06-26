from __future__ import annotations

import uuid

from .clients import BarsProvider, CardsProvider
from .engine import BacktesterEngine
from .models import BacktestRunParams
from .repository import BacktesterRepository, dataset_snapshot_hash
from .settings import BacktesterSettings


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
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._cards = cards_provider
        self._bars = bars_provider

    def run(self, params: BacktestRunParams) -> None:
        self._validate_params(params)
        cards = self._cards.export_cards(
            window_start_at=params.window_start_at,
            window_end_at=params.window_end_at,
        )
        snapshot_hash = dataset_snapshot_hash(cards)
        self._repository.create_run(params=params, dataset_snapshot_hash=snapshot_hash)

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

    def _validate_params(self, params: BacktestRunParams) -> None:
        if params.window_start_at >= params.window_end_at:
            raise ValueError("invalid_window")
        if params.ideal_fetch_delay_seconds < 0 or params.ideal_thesis_delay_seconds < 0:
            raise ValueError("invalid_ideal_delays")
