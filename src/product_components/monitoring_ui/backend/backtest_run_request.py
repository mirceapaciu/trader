from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BacktestRunRequest:
    """Plain coordinator input, decoupled from the Pydantic request model."""

    window_start_at: datetime
    window_end_at: datetime
    mode: str = "replay"
    timing_scenario: str = "ideal"
    card_population: str = "all"
    strategies: list[str] | None = None
    initial_capital: float | None = None
    run_note: str | None = None
    # Regeneration mode only: which OpenAI model to re-run analysis with. None ->
    # the coordinator falls back to the production default model.
    llm_model: str | None = None
    # Regeneration mode only: ThesisBuilder evidence-threshold overrides. None ->
    # the production ThesisBuilder default is used.
    required_evidence_count: int | None = None
    evidence_collection_max_minutes: int | None = None
