from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from .clients import BarsProvider, CardsProvider, RegenerationProvider
from .engine import BacktesterEngine
from .models import BacktestMode, BacktestRunParams
from .repository import BacktesterRepository, dataset_snapshot_hash, sim_schema_name
from .settings import BacktesterSettings

LOGGER = logging.getLogger("backtester")

# Reports run progress to an external observer: (phase, done, total, ticker) where phase is
# "prewarming" | "regenerating" | "simulating".
ProgressSink = Callable[[str, int, int, "str | None"], None]


def new_run_id() -> str:
    return f"bt_{uuid.uuid4().hex}"


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parents[3]


class BacktesterService:
    def __init__(
        self,
        *,
        settings: BacktesterSettings,
        repository: BacktesterRepository,
        cards_provider: CardsProvider,
        bars_provider: BarsProvider,
        regeneration_provider: RegenerationProvider | None = None,
        progress: ProgressSink | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._cards = cards_provider
        self._bars = bars_provider
        self._regeneration = regeneration_provider
        self._progress = progress
        self._repo_root = repo_root or _repo_root_default()

    def run(self, params: BacktestRunParams) -> None:
        self._validate_params(params)
        if params.mode == BacktestMode.REGENERATION:
            self._run_regeneration(params)
        else:
            self._run_replay(params)

    # ----- replay ---------------------------------------------------------

    def _run_replay(self, params: BacktestRunParams) -> None:
        cards = self._cards.export_cards(
            window_start_at=params.window_start_at,
            window_end_at=params.window_end_at,
        )
        snapshot_hash = dataset_snapshot_hash(cards)
        self._repository.create_run(params=params, dataset_snapshot_hash=snapshot_hash)

        self._prefetch_market_data(cards, params)

        self._report_progress("simulating", 0, 0, None)
        try:
            self._run_engine_and_persist(params, self._cards)
        except Exception as error:
            self._repository.finalize_run_failure(
                run_id=params.run_id,
                error_code=error.__class__.__name__,
                details={"message": str(error)[:1000]},
            )
            raise

    # ----- regeneration ---------------------------------------------------

    def _run_regeneration(self, params: BacktestRunParams) -> None:
        if not self._settings.regeneration_enabled:
            raise RuntimeError("regeneration_disabled")
        if self._regeneration is None:
            raise RuntimeError("regeneration_provider_unavailable")

        # Isolated per-run schema: regenerated analyses/cards never touch production.
        sim_schema = sim_schema_name(params.run_id)
        self._repository.bootstrap_sim_thesis_schema(
            repo_root=self._repo_root, sim_schema=sim_schema
        )

        thesis_config = self._regeneration.thesis_config_snapshot(
            llm_model=params.llm_model
        )
        token_budget = params.llm_max_tokens_per_run
        snapshot_hash = _regeneration_dataset_hash(params=params, thesis_config=thesis_config)
        # Record the run up front (status running) so hard failures are persisted.
        self._repository.create_run(
            params=params,
            dataset_snapshot_hash=snapshot_hash,
            thesis_config_snapshot=thesis_config,
            llm_token_budget_limit=token_budget,
            llm_model=params.llm_model,
        )

        try:
            card_delay = (
                params.ideal_fetch_delay_seconds + params.ideal_thesis_delay_seconds
            )
            self._report_progress("regenerating", 0, 0, None)
            self._regeneration.regenerate(
                run_id=params.run_id,
                sim_schema=sim_schema,
                window_start_at=params.window_start_at,
                window_end_at=params.window_end_at,
                llm_model=params.llm_model,
                token_budget=token_budget,
                card_delay_seconds=card_delay,
                progress=lambda done, total, ticker: self._report_progress(
                    "regenerating", done, total, ticker
                ),
            )

            sim_cards = self._regeneration.cards_provider(sim_schema=sim_schema)
            cards = sim_cards.export_cards(
                window_start_at=params.window_start_at,
                window_end_at=params.window_end_at,
            )
            self._prefetch_market_data(cards, params)

            self._report_progress("simulating", 0, 0, None)
            self._run_engine_and_persist(params, sim_cards)
        except Exception as error:
            self._repository.finalize_run_failure(
                run_id=params.run_id,
                error_code=error.__class__.__name__,
                details={"message": str(error)[:1000]},
            )
            raise

    # ----- shared simulation/persistence ---------------------------------

    def _run_engine_and_persist(
        self, params: BacktestRunParams, cards_provider: CardsProvider
    ) -> None:
        result = BacktesterEngine(
            params=params,
            cards_provider=cards_provider,
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
        if params.mode == BacktestMode.REGENERATION:
            if not params.llm_model.strip():
                raise ValueError("invalid_llm_model")
            if params.llm_max_tokens_per_run <= 0:
                raise ValueError("invalid_token_budget")


def _regeneration_dataset_hash(
    *, params: BacktestRunParams, thesis_config: dict
) -> str:
    """Deterministic id for a regeneration run's immutable inputs.

    The dataset for a regeneration run is defined by its window plus the ThesisBuilder
    config snapshot (model + thresholds), not by pre-existing cards, so the snapshot
    hash is derived from those parameters.
    """
    payload = {
        "mode": params.mode.value,
        "window_start_at": params.window_start_at.isoformat(),
        "window_end_at": params.window_end_at.isoformat(),
        "thesis_config": thesis_config,
    }
    normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
