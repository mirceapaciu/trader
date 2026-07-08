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
        # The provider is required to build the ThesisBuilder config snapshot the
        # run row needs; its absence is a wiring error, not a runtime condition.
        if self._regeneration is None:
            raise RuntimeError("regeneration_provider_unavailable")

        sim_schema = sim_schema_name(params.run_id)
        thesis_config = self._regeneration.thesis_config_snapshot(
            llm_model=params.llm_model,
            required_evidence_count=params.required_evidence_count,
            evidence_collection_max_minutes=params.evidence_collection_max_minutes,
        )
        token_budget = params.llm_max_tokens_per_run
        snapshot_hash = _regeneration_dataset_hash(params=params, thesis_config=thesis_config)
        # Create the run row FIRST so that every subsequent failure (including a
        # disabled-regeneration config error) is persisted as a `failed` run the
        # UI can display, instead of leaving the UI polling a run_id that never
        # gets a DB row (which looks "stuck").
        self._repository.create_run(
            params=params,
            dataset_snapshot_hash=snapshot_hash,
            thesis_config_snapshot=thesis_config,
            llm_token_budget_limit=token_budget,
            llm_model=params.llm_model,
        )
        LOGGER.info(
            "regeneration run created run_id=%s window=%s..%s model=%s token_budget=%d",
            params.run_id,
            params.window_start_at.isoformat(),
            params.window_end_at.isoformat(),
            params.llm_model,
            token_budget,
        )

        try:
            if not self._settings.regeneration_enabled:
                raise RuntimeError("regeneration_disabled")

            self._report_progress("regenerating", 0, 0, None)
            self._repository.bootstrap_sim_thesis_schema(
                repo_root=self._repo_root, sim_schema=sim_schema
            )
            card_delay = (
                params.ideal_fetch_delay_seconds + params.ideal_thesis_delay_seconds
            )
            LOGGER.info("regeneration analysis starting run_id=%s sim_schema=%s", params.run_id, sim_schema)
            regen = self._regeneration.regenerate(
                run_id=params.run_id,
                sim_schema=sim_schema,
                window_start_at=params.window_start_at,
                window_end_at=params.window_end_at,
                llm_model=params.llm_model,
                token_budget=token_budget,
                card_delay_seconds=card_delay,
                required_evidence_count=params.required_evidence_count,
                evidence_collection_max_minutes=params.evidence_collection_max_minutes,
                progress=lambda done, total, ticker: self._report_progress(
                    "regenerating", done, total, ticker
                ),
            )
            LOGGER.info(
                "regeneration analysis done run_id=%s articles=%d analyses=%d cards=%d budget_exhausted=%s",
                params.run_id,
                regen.articles_found,
                regen.analyses_created,
                regen.cards_created,
                regen.budget_exhausted,
            )

            sim_cards = self._regeneration.cards_provider(sim_schema=sim_schema)
            cards = sim_cards.export_cards(
                window_start_at=params.window_start_at,
                window_end_at=params.window_end_at,
            )
            LOGGER.info(
                "regeneration prewarming market data run_id=%s cards=%d", params.run_id, len(cards)
            )
            self._prefetch_market_data(cards, params)

            regen_stats = {
                "articles_found": regen.articles_found,
                "articles_relevant": regen.articles_relevant,
                "articles_analyzed": regen.articles_analyzed,
                "analyses_created": regen.analyses_created,
                "cards_created": regen.cards_created,
                "llm_tokens_used": regen.llm_tokens_used,
                "llm_cache_hits": regen.llm_cache_hits,
                "llm_calls": regen.llm_calls,
                "evidence_windows_created": self._repository.count_sim_evidence_windows(
                    sim_schema=sim_schema
                ),
                "budget_exhausted": regen.budget_exhausted,
            }

            self._report_progress("simulating", 0, 0, None)
            LOGGER.info("regeneration simulating run_id=%s", params.run_id)
            self._run_engine_and_persist(
                params, sim_cards, extra_summary={"regeneration": regen_stats}
            )
            LOGGER.info("regeneration run completed run_id=%s", params.run_id)
        except Exception as error:
            LOGGER.exception("regeneration run failed run_id=%s", params.run_id)
            self._repository.finalize_run_failure(
                run_id=params.run_id,
                error_code=error.__class__.__name__,
                details={"message": str(error)[:1000]},
            )
            raise

    # ----- shared simulation/persistence ---------------------------------

    def _run_engine_and_persist(
        self,
        params: BacktestRunParams,
        cards_provider: CardsProvider,
        *,
        extra_summary: dict | None = None,
    ) -> None:
        result = BacktesterEngine(
            params=params,
            cards_provider=cards_provider,
            bars_provider=self._bars,
        ).run()

        if extra_summary:
            # Merge regeneration counters into the run summary the UI reads.
            result.metrics.summary_json.update(extra_summary)

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
