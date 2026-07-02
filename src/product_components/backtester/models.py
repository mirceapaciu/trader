from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class BacktestMode(StrEnum):
    REPLAY = "replay"
    REGENERATION = "regeneration"


class TimingScenario(StrEnum):
    IDEAL = "ideal"
    ACTUAL = "actual"
    BOTH = "both"


class CardPopulation(StrEnum):
    ALL = "all"
    APPROVED_ONLY = "approved_only"
    REJECTED_ONLY = "rejected_only"


class ExitReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    REVERSAL = "reversal"
    WINDOW_END = "window_end"
    NOT_FILLED = "not_filled"
    RISK_BLOCKED = "risk_blocked"


@dataclass(frozen=True)
class ExecutionModel:
    commission_model: str = "per_share"
    commission_per_share_usd: float = 0.005
    commission_min_usd: float = 1.0
    commission_flat_usd: float = 0.0
    entry_slippage_bps: float = 5.0
    exit_slippage_bps: float = 5.0
    limit_order_validity_bars: int = 3
    intrabar_stop_before_target: bool = True
    bar_interval: str = "1m"
    # Default exit thresholds derived from docs/trading-strategies.md Sentiment
    # Momentum (the bread-and-butter strategy): take-profit +3%, stop -1.5%,
    # time stop 4h. Parameterizable per run.
    take_profit_pct: float = 0.03
    stop_loss_pct: float = 0.015
    time_stop_seconds: int = 4 * 60 * 60

    def snapshot(self) -> dict[str, Any]:
        return {
            "commission_model": self.commission_model,
            "commission_per_share_usd": self.commission_per_share_usd,
            "commission_min_usd": self.commission_min_usd,
            "commission_flat_usd": self.commission_flat_usd,
            "entry_slippage_bps": self.entry_slippage_bps,
            "exit_slippage_bps": self.exit_slippage_bps,
            "limit_order_validity_bars": self.limit_order_validity_bars,
            "intrabar_stop_before_target": self.intrabar_stop_before_target,
            "bar_interval": self.bar_interval,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "time_stop_seconds": self.time_stop_seconds,
        }


@dataclass(frozen=True)
class RiskModel:
    max_position_usd: float = 1000.0
    max_portfolio_exposure_usd: float = 5000.0
    max_positions_per_sector: int = 3
    max_daily_trades: int = 10
    daily_loss_limit_usd: float = 200.0
    ticker_cooldown_minutes: int = 30

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_position_usd": self.max_position_usd,
            "max_portfolio_exposure_usd": self.max_portfolio_exposure_usd,
            "max_positions_per_sector": self.max_positions_per_sector,
            "max_daily_trades": self.max_daily_trades,
            "daily_loss_limit_usd": self.daily_loss_limit_usd,
            "ticker_cooldown_minutes": self.ticker_cooldown_minutes,
        }


@dataclass(frozen=True)
class BacktestRunParams:
    run_id: str
    window_start_at: datetime
    window_end_at: datetime
    mode: BacktestMode = BacktestMode.REPLAY
    timing_scenario: TimingScenario = TimingScenario.IDEAL
    card_population: CardPopulation = CardPopulation.ALL
    strategies: list[str] | None = None
    initial_capital: float = 10000.0
    ideal_fetch_delay_seconds: int = 120
    ideal_thesis_delay_seconds: int = 60
    execution_model: ExecutionModel = field(default_factory=ExecutionModel)
    risk_model: RiskModel = field(default_factory=RiskModel)
    risk_free_rate_per_period: float = 0.0
    run_note: str | None = None
    # Regeneration mode only: the OpenAI model used to re-run ThesisBuilder
    # analysis. Ignored in replay mode. Defaults to the production model.
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens_per_run: int = 500000
    # Regeneration mode only: ThesisBuilder evidence thresholds. None means use the
    # production ThesisBuilder default. Overriding lets an operator explore how many
    # cards a wider/looser evidence policy would have produced over the window.
    required_evidence_count: int | None = None
    evidence_collection_max_minutes: int | None = None


@dataclass(frozen=True)
class CardSnapshot:
    snapshot_id: str
    run_id: str
    thesis_card_id: str
    ticker: str
    exchange_code: str
    direction: str
    strategy: str
    time_horizon: str
    confidence: float
    decision_state: str
    card_created_at: datetime
    card_expires_at: datetime
    evidence_json: list[dict[str, Any]]
    news_ready_at: datetime
    risk_box_json: dict[str, Any]
    source_export_ref: str | None = None


@dataclass(frozen=True)
class SimulatedTrade:
    trade_id: str
    run_id: str
    thesis_card_id: str
    ticker: str
    exchange_code: str
    strategy: str
    direction: str
    card_decision_state: str
    card_was_live_expired: bool
    entry_timing_scenario: str
    news_published_at: datetime | None
    news_fetched_at: datetime | None
    card_created_at: datetime
    news_fetch_delay_seconds: float | None
    thesis_build_delay_seconds: float | None
    total_pipeline_delay_seconds: float | None
    entry_at: datetime | None
    entry_price: float | None
    quantity: float | None
    exit_at: datetime | None
    exit_price: float | None
    gross_pnl: float | None
    commission: float | None
    slippage: float | None
    net_pnl: float | None
    return_pct: float | None
    exit_reason: ExitReason
    risk_block_rule: str | None
    holding_period_seconds: float | None

    @property
    def is_closed(self) -> bool:
        return self.exit_reason not in (ExitReason.NOT_FILLED, ExitReason.RISK_BLOCKED)


@dataclass(frozen=True)
class EquityPoint:
    point_id: str
    run_id: str
    timing_scenario: str
    as_of: datetime
    equity: float
    open_positions: int


@dataclass(frozen=True)
class RunMetrics:
    cards_considered: int
    cards_in_population: int
    cards_live_executable: int
    cards_skipped_no_price: int
    trades_opened: int
    trades_closed: int
    trades_risk_blocked: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    total_commission: float
    total_slippage: float
    total_return: float | None
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    max_drawdown: float | None
    max_drawdown_duration_seconds: float | None
    sharpe_ratio: float | None
    exposure_fraction: float | None
    signal_accuracy: float | None
    avg_news_fetch_delay_seconds: float | None
    p95_news_fetch_delay_seconds: float | None
    max_news_fetch_delay_seconds: float | None
    avg_thesis_build_delay_seconds: float | None
    p95_thesis_build_delay_seconds: float | None
    max_thesis_build_delay_seconds: float | None
    avg_total_pipeline_delay_seconds: float | None
    p95_total_pipeline_delay_seconds: float | None
    max_total_pipeline_delay_seconds: float | None
    pnl_gap: float | None
    win_rate_gap: float | None
    trades_flipped_by_delay: int | None
    summary_json: dict[str, Any] = field(default_factory=dict)
    summary_md: str = ""


@dataclass(frozen=True)
class BacktestResult:
    card_snapshots: list[CardSnapshot]
    trades: list[SimulatedTrade]
    equity_points: list[EquityPoint]
    metrics: RunMetrics
