from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.core_components.backtest_engine import (
    Bar,
    BarSeries,
    CommissionModel,
    apply_slippage,
    expectancy,
    max_drawdown,
    max_drawdown_with_duration,
    percentile,
    profit_factor,
    round_half_up,
    sharpe_ratio,
    win_rate,
)
from src.product_components.thesis_builder.export import ExportedThesisCard
from src.product_components.trade_executor.models import (
    DecisionReason,
    ThesisCard,
)
from src.product_components.trade_executor.pipeline import (
    DailyRiskState,
    PortfolioState,
    construct_levels,
    entry_limit_price,
    evaluate_admission_gate,
    evaluate_risk_gate,
    size_position,
)
from src.product_components.trade_executor.trading_calendar import (
    TradingCalendar,
    build_default_calendar,
)

from .clients import BarsProvider, CardsProvider
from .models import (
    BacktestResult,
    BacktestRunParams,
    CardPopulation,
    CardSnapshot,
    EquityPoint,
    ExecutionMode,
    ExecutionModel,
    ExitReason,
    RiskModel,
    RunMetrics,
    SimulatedTrade,
    TimingScenario,
)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decision_state(card: ExportedThesisCard) -> str:
    # Thesis cards persist validation_status IN ('valid', 'rejected').
    # 'valid' is the live-approved/executable state; 'rejected' otherwise.
    return "approved" if card.validation_status == "valid" else "rejected"


def _to_trade_executor_card(card: ExportedThesisCard) -> ThesisCard:
    return ThesisCard(
        thesis_card_id=card.id,
        ticker=card.ticker,
        exchange_code=card.exchange_code,
        direction=card.direction,
        time_horizon=card.time_horizon,
        strategy=card.strategy,
        confidence=card.confidence,
        max_loss_usd=card.risk_max_loss_usd,
        stop_condition=card.risk_stop_condition,
        invalidation_condition=card.risk_invalidation_condition,
        source_analysis_ids=[],
        created_at=_to_utc(card.created_at),
        expires_at=_to_utc(card.expires_at),
    )


@dataclass(frozen=True)
class _OpenPosition:
    notional: float
    exit_at: datetime
    ticker_key: str


@dataclass(frozen=True)
class _CardTiming:
    news_ready_at: datetime
    t_ideal: datetime
    t_actual: datetime
    news_published_at: datetime | None
    news_fetched_at: datetime | None
    news_fetch_delay_seconds: float | None
    thesis_build_delay_seconds: float | None
    total_pipeline_delay_seconds: float | None


@dataclass(frozen=True)
class _ExecutionPlan:
    quantity: float
    notional: float
    entry_price: float
    stop_price: float
    target_price: float
    time_exit_at: datetime


class BacktesterEngine:
    """Pure, deterministic backtest simulator with no DB or provider access.

    All external data arrives through the injected ``cards_provider`` and
    ``bars_provider`` so the engine can be unit-tested with fakes.
    """

    def __init__(
        self,
        *,
        params: BacktestRunParams,
        cards_provider: CardsProvider,
        bars_provider: BarsProvider,
        trading_calendar: TradingCalendar | None = None,
    ) -> None:
        self._params = params
        self._cards = cards_provider
        self._bars = bars_provider
        self._calendar = trading_calendar or build_default_calendar()

    def run(self) -> BacktestResult:
        params = self._params
        cards = self._cards.export_cards(
            window_start_at=params.window_start_at,
            window_end_at=params.window_end_at,
        )
        cards = sorted(cards, key=lambda c: (_to_utc(c.created_at), c.id))
        cards_considered = len(cards)

        population = self._filter_population(cards)
        cards_live_executable = sum(
            1
            for card in population
            if _decision_state(card) == "approved"
            and not self._was_live_expired(card, self._card_timing(card).t_ideal)
        )

        snapshots = [self._build_snapshot(card) for card in population]

        scenarios = self._scenarios()
        per_scenario_trades: dict[str, list[SimulatedTrade]] = {}
        per_scenario_equity: dict[str, list[EquityPoint]] = {}
        cards_skipped_no_price = 0

        for scenario in scenarios:
            trades, equity, skipped = self._simulate_scenario(scenario, population)
            per_scenario_trades[scenario] = trades
            per_scenario_equity[scenario] = equity
            # Count skipped only once (on the primary scenario) to avoid double counting.
            if scenario == scenarios[0]:
                cards_skipped_no_price = skipped

        all_trades: list[SimulatedTrade] = []
        all_equity: list[EquityPoint] = []
        for scenario in scenarios:
            all_trades.extend(per_scenario_trades[scenario])
            all_equity.extend(per_scenario_equity[scenario])

        metrics = self._build_metrics(
            cards_considered=cards_considered,
            cards_in_population=len(population),
            cards_live_executable=cards_live_executable,
            cards_skipped_no_price=cards_skipped_no_price,
            scenarios=scenarios,
            population=population,
            per_scenario_trades=per_scenario_trades,
            per_scenario_equity=per_scenario_equity,
        )
        return BacktestResult(
            card_snapshots=snapshots,
            trades=all_trades,
            equity_points=all_equity,
            metrics=metrics,
        )

    # ----- card selection -------------------------------------------------

    def _filter_population(self, cards: list[ExportedThesisCard]) -> list[ExportedThesisCard]:
        population = self._params.card_population
        strategies = (
            {s for s in self._params.strategies} if self._params.strategies else None
        )
        result: list[ExportedThesisCard] = []
        for card in cards:
            state = _decision_state(card)
            if population == CardPopulation.APPROVED_ONLY and state != "approved":
                continue
            if population == CardPopulation.REJECTED_ONLY and state != "rejected":
                continue
            if strategies is not None and card.strategy not in strategies:
                continue
            result.append(card)
        return result

    def _scenarios(self) -> list[str]:
        scenario = self._params.timing_scenario
        if scenario == TimingScenario.BOTH:
            return [TimingScenario.IDEAL.value, TimingScenario.ACTUAL.value]
        return [scenario.value]

    # ----- timing ---------------------------------------------------------

    def _card_timing(self, card: ExportedThesisCard) -> _CardTiming:
        news_ready_at = _to_utc(card.news_ready_at)
        created_at = _to_utc(card.created_at)
        t_ideal = news_ready_at + timedelta(
            seconds=self._params.ideal_fetch_delay_seconds
            + self._params.ideal_thesis_delay_seconds
        )
        # Triggering evidence: the article whose published_at == news_ready_at;
        # on ties take the latest fetched_at (most conservative latency).
        triggering = None
        for article in card.evidence:
            if _to_utc(article.published_at) != news_ready_at:
                continue
            if triggering is None or _to_utc(article.fetched_at) > _to_utc(
                triggering.fetched_at
            ):
                triggering = article

        news_published_at = (
            _to_utc(triggering.published_at) if triggering is not None else None
        )
        news_fetched_at = (
            _to_utc(triggering.fetched_at) if triggering is not None else None
        )
        news_fetch_delay = (
            (news_fetched_at - news_published_at).total_seconds()
            if news_published_at is not None and news_fetched_at is not None
            else None
        )
        # Clamp to None when timestamps are causally inconsistent (e.g. article
        # re-fetched after the card was already created).
        thesis_build_delay_raw = (
            (created_at - news_fetched_at).total_seconds()
            if news_fetched_at is not None
            else None
        )
        thesis_build_delay = (
            thesis_build_delay_raw if thesis_build_delay_raw is None or thesis_build_delay_raw >= 0 else None
        )
        total_pipeline_delay_raw = (
            (created_at - news_published_at).total_seconds()
            if news_published_at is not None
            else None
        )
        total_pipeline_delay = (
            total_pipeline_delay_raw if total_pipeline_delay_raw is None or total_pipeline_delay_raw >= 0 else None
        )
        return _CardTiming(
            news_ready_at=news_ready_at,
            t_ideal=t_ideal,
            t_actual=created_at,
            news_published_at=news_published_at,
            news_fetched_at=news_fetched_at,
            news_fetch_delay_seconds=news_fetch_delay,
            thesis_build_delay_seconds=thesis_build_delay,
            total_pipeline_delay_seconds=total_pipeline_delay,
        )

    def _entry_time(self, scenario: str, timing: _CardTiming) -> datetime:
        if scenario == TimingScenario.IDEAL.value:
            return timing.t_ideal
        return timing.t_actual

    def _was_live_expired(self, card: ExportedThesisCard, t_entry: datetime) -> bool:
        return t_entry > _to_utc(card.expires_at)

    # ----- snapshots ------------------------------------------------------

    def _build_snapshot(self, card: ExportedThesisCard) -> CardSnapshot:
        timing = self._card_timing(card)
        evidence_json = [
            {
                "article_id": article.article_id,
                "published_at": _to_utc(article.published_at).isoformat(),
                "fetched_at": _to_utc(article.fetched_at).isoformat(),
            }
            for article in card.evidence
        ]
        risk_box_json = {
            "max_loss_usd": card.risk_max_loss_usd,
            "stop_condition": card.risk_stop_condition,
            "invalidation_condition": card.risk_invalidation_condition,
        }
        return CardSnapshot(
            snapshot_id=_snapshot_id(self._params.run_id, card.id),
            run_id=self._params.run_id,
            thesis_card_id=card.id,
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            direction=card.direction,
            strategy=card.strategy,
            time_horizon=card.time_horizon,
            confidence=card.confidence,
            decision_state=_decision_state(card),
            card_created_at=_to_utc(card.created_at),
            card_expires_at=_to_utc(card.expires_at),
            evidence_json=evidence_json,
            news_ready_at=timing.news_ready_at,
            risk_box_json=risk_box_json,
            source_export_ref=None,
        )

    # ----- per-scenario simulation ---------------------------------------

    def _simulate_scenario(
        self,
        scenario: str,
        population: list[ExportedThesisCard],
    ) -> tuple[list[SimulatedTrade], list[EquityPoint], int]:
        params = self._params
        execution = params.execution_model
        risk = params.risk_model
        commission_model = CommissionModel(
            model=execution.commission_model,
            per_share_usd=execution.commission_per_share_usd,
            min_usd=execution.commission_min_usd,
            flat_usd=execution.commission_flat_usd,
        )

        # Build per-card entry plans ordered by entry time (deterministic).
        plans: list[tuple[datetime, ExportedThesisCard, _CardTiming]] = []
        skipped = 0
        for card in population:
            timing = self._card_timing(card)
            t_entry = self._entry_time(scenario, timing)
            plans.append((t_entry, card, timing))
        plans.sort(key=lambda item: (item[0], item[1].id))

        # Reversal map: later opposing approved card for the same instrument.
        reversal_after: dict[str, list[tuple[datetime, str]]] = {}
        for t_entry, card, _timing in plans:
            if _decision_state(card) == "approved":
                reversal_after.setdefault(
                    f"{card.ticker}|{card.exchange_code}", []
                ).append((t_entry, card.direction))

        # Portfolio state. Positions are entered and exited within this single
        # chronological pass; ``open_positions`` holds positions whose exit time
        # is after the current entry time so concurrent-exposure caps apply.
        trades: list[SimulatedTrade] = []
        open_positions: list[_OpenPosition] = []
        daily_trade_count: dict[str, int] = {}
        daily_realized_pnl: dict[str, float] = {}
        daily_halted: dict[str, bool] = {}
        last_trade_at: dict[str, datetime] = {}
        cooldown = timedelta(minutes=risk.ticker_cooldown_minutes)
        bars_cache: dict[str, BarSeries] = {}

        for t_entry, card, timing in plans:
            ticker_key = f"{card.ticker}|{card.exchange_code}"

            # Release exposure of positions that have already exited by t_entry.
            open_positions = [p for p in open_positions if p.exit_at > t_entry]

            series = bars_cache.get(ticker_key)
            if series is None:
                bars = self._bars.historical_bars(
                    ticker=card.ticker,
                    exchange_code=card.exchange_code,
                    interval=execution.bar_interval,
                    start=t_entry,
                    end=params.window_end_at,
                )
                series = BarSeries(bars)
                bars_cache[ticker_key] = series

            if series.is_empty or series.first_at_or_after(t_entry) is None:
                trades.append(
                    self._skipped_trade(
                        card, scenario, timing, ExitReason.NOT_FILLED, t_entry
                    )
                )
                skipped += 1
                continue

            day_key = t_entry.date().isoformat()
            execution_mode = execution.execution_mode

            # Portfolio/risk gates (binding constraint => risk_blocked).
            block_rule = None
            prev_trade = last_trade_at.get(ticker_key)
            current_exposure = sum(p.notional for p in open_positions)
            has_open_or_working_position = any(
                p.ticker_key == ticker_key for p in open_positions
            )
            if execution_mode == ExecutionMode.LEGACY_FLAT_PERCENT and prev_trade is not None and t_entry - prev_trade < cooldown:
                block_rule = "ticker_cooldown"
            elif execution_mode == ExecutionMode.LEGACY_FLAT_PERCENT and daily_trade_count.get(day_key, 0) >= risk.max_daily_trades:
                block_rule = "max_daily_trades"
            elif execution_mode == ExecutionMode.LEGACY_FLAT_PERCENT and daily_realized_pnl.get(day_key, 0.0) <= -risk.daily_loss_limit_usd:
                block_rule = "daily_loss_limit"
            elif execution_mode == ExecutionMode.LEGACY_FLAT_PERCENT and current_exposure >= risk.max_portfolio_exposure_usd:
                block_rule = "max_portfolio_exposure"

            if block_rule is not None:
                trades.append(
                    self._risk_blocked_trade(card, scenario, timing, block_rule)
                )
                continue

            # Entry fill (market order at first bar at/after t_entry open).
            entry_bar = series.first_at_or_after(t_entry)
            if execution_mode == ExecutionMode.LIVE_PARITY:
                plan_or_rule = self._live_parity_execution_plan(
                    card=card,
                    entry_bar=entry_bar,
                    t_entry=t_entry,
                    execution=execution,
                    risk=risk,
                    current_exposure=current_exposure,
                    open_positions=open_positions,
                    has_open_or_working_position=has_open_or_working_position,
                    daily_trade_count=daily_trade_count,
                    daily_realized_pnl=daily_realized_pnl,
                    daily_halted=daily_halted,
                    day_key=day_key,
                )
                if isinstance(plan_or_rule, str):
                    trades.append(
                        self._risk_blocked_trade(card, scenario, timing, plan_or_rule)
                    )
                    continue
                plan = plan_or_rule
            else:
                quantity = self._position_size(card, entry_bar.open, risk)
                notional = quantity * entry_bar.open
                if notional > risk.max_portfolio_exposure_usd - current_exposure:
                    # Per-position portfolio share cap binds for this candidate.
                    trades.append(
                        self._risk_blocked_trade(
                            card, scenario, timing, "max_portfolio_exposure"
                        )
                    )
                    continue
                entry_price = apply_slippage(
                    entry_bar.open,
                    execution.entry_slippage_bps,
                    worse_is_higher=(card.direction == "buy"),
                )
                plan = _ExecutionPlan(
                    quantity=quantity,
                    notional=notional,
                    entry_price=entry_price,
                    stop_price=self._stop_price(entry_price, card.direction, execution),
                    target_price=self._target_price(entry_price, card.direction, execution),
                    time_exit_at=_to_utc(entry_bar.start_at)
                    + timedelta(seconds=execution.time_stop_seconds),
                )

            direction = card.direction
            entry_price = plan.entry_price
            quantity = plan.quantity
            notional = plan.notional
            entry_commission = commission_model.cost(quantity, entry_price)

            exit_bar, exit_reason = self._find_exit(
                series=series,
                entry_bar=entry_bar,
                direction=direction,
                execution=execution,
                stop=plan.stop_price,
                target=plan.target_price,
                time_exit_at=plan.time_exit_at,
                ticker_key=ticker_key,
                reversal_after=reversal_after,
            )

            exit_price_raw = exit_bar.close if exit_reason != ExitReason.STOP_LOSS else (
                plan.stop_price
            )
            if exit_reason == ExitReason.TAKE_PROFIT:
                exit_price_raw = plan.target_price
            exit_price = apply_slippage(
                exit_price_raw,
                execution.exit_slippage_bps,
                worse_is_higher=(direction == "sell"),
            )
            exit_commission = commission_model.cost(quantity, exit_price)
            commission = entry_commission + exit_commission

            if direction == "buy":
                gross_pnl = (exit_price - entry_price) * quantity
            else:
                gross_pnl = (entry_price - exit_price) * quantity

            # Slippage cost (difference vs the un-slipped reference prices).
            entry_slip = abs(entry_price - entry_bar.open) * quantity
            exit_slip = abs(exit_price - exit_price_raw) * quantity
            slippage = entry_slip + exit_slip
            net_pnl = gross_pnl - commission
            return_pct = net_pnl / notional if notional else None
            holding = (_to_utc(exit_bar.start_at) - _to_utc(entry_bar.start_at)).total_seconds()

            trade = SimulatedTrade(
                trade_id=_trade_id(self._params.run_id, card.id, scenario),
                run_id=self._params.run_id,
                thesis_card_id=card.id,
                ticker=card.ticker,
                exchange_code=card.exchange_code,
                strategy=card.strategy,
                direction=direction,
                card_decision_state=_decision_state(card),
                card_was_live_expired=self._was_live_expired(card, t_entry),
                entry_timing_scenario=scenario,
                news_published_at=timing.news_published_at,
                news_fetched_at=timing.news_fetched_at,
                card_created_at=timing.t_actual,
                news_fetch_delay_seconds=timing.news_fetch_delay_seconds,
                thesis_build_delay_seconds=timing.thesis_build_delay_seconds,
                total_pipeline_delay_seconds=timing.total_pipeline_delay_seconds,
                entry_at=_to_utc(entry_bar.start_at),
                entry_price=entry_price,
                quantity=quantity,
                exit_at=_to_utc(exit_bar.start_at),
                exit_price=exit_price,
                gross_pnl=gross_pnl,
                commission=commission,
                slippage=slippage,
                net_pnl=net_pnl,
                return_pct=return_pct,
                exit_reason=exit_reason,
                risk_block_rule=None,
                holding_period_seconds=holding,
            )
            trades.append(trade)
            open_positions.append(
                _OpenPosition(
                    notional=notional,
                    exit_at=_to_utc(exit_bar.start_at),
                    ticker_key=ticker_key,
                )
            )

            daily_trade_count[day_key] = daily_trade_count.get(day_key, 0) + 1
            daily_realized_pnl[day_key] = daily_realized_pnl.get(day_key, 0.0) + net_pnl
            last_trade_at[ticker_key] = t_entry

        equity_points = self._build_equity_points(scenario, trades)
        return trades, equity_points, skipped

    def _position_size(
        self, card: ExportedThesisCard, price: float, risk: RiskModel
    ) -> float:
        # Fixed-fractional sizing keyed on confidence (trading-strategies Section 6):
        # confidence >= 0.8 -> 100% of max position; 0.6-0.8 -> 50%; else 50% floor.
        if card.confidence >= 0.8:
            budget = risk.max_position_usd
        elif card.confidence >= 0.6:
            budget = risk.max_position_usd * 0.5
        else:
            budget = risk.max_position_usd * 0.5
        if price <= 0:
            return 0.0
        return budget / price

    def _live_parity_execution_plan(
        self,
        *,
        card: ExportedThesisCard,
        entry_bar: Bar,
        t_entry: datetime,
        execution: ExecutionModel,
        risk: RiskModel,
        current_exposure: float,
        open_positions: list[_OpenPosition],
        has_open_or_working_position: bool,
        daily_trade_count: dict[str, int],
        daily_realized_pnl: dict[str, float],
        daily_halted: dict[str, bool],
        day_key: str,
    ) -> _ExecutionPlan | str:
        thesis_card = _to_trade_executor_card(card)
        admission = evaluate_admission_gate(
            card=thesis_card,
            now=t_entry,
            min_confidence=execution.min_confidence,
            in_watchlist=True,
            review_state="approved" if _decision_state(card) == "approved" else "rejected",
            has_open_or_working_position=has_open_or_working_position,
            horizon_map=execution.time_horizon_days_map,
        )
        if not admission.passed:
            return admission.reason.value

        entry = entry_limit_price(
            direction=card.direction,
            bid=entry_bar.open,
            ask=entry_bar.open,
            slippage_bps=execution.entry_limit_slippage_bps,
        )
        atr_20d = self._atr_20d_before_entry(card, t_entry)
        if atr_20d is None:
            return DecisionReason.ATR_UNAVAILABLE.value

        levels = construct_levels(
            direction=card.direction,
            entry=entry,
            atr_20d=atr_20d,
            atr_stop_mult=execution.atr_stop_mult,
            take_profit_r=execution.take_profit_r,
        )
        order = size_position(
            max_loss_usd=card.risk_max_loss_usd,
            entry=levels.entry,
            stop=levels.stop,
            max_position_size=risk.max_position_usd,
            portfolio_headroom=risk.max_portfolio_exposure_usd - current_exposure,
        )
        if order.quantity < 1:
            return DecisionReason.SIZE_BELOW_ONE_SHARE.value

        daily = DailyRiskState(
            realized_pnl=daily_realized_pnl.get(day_key, 0.0),
            unrealized_pnl=0.0,
            trades_count=daily_trade_count.get(day_key, 0),
            halted=daily_halted.get(day_key, False),
        )
        portfolio = PortfolioState(
            open_and_working_count=len(open_positions),
            deployed_capital=current_exposure,
            sector_exposure=0.0,
            sector_known=False,
        )
        risk_gate = evaluate_risk_gate(
            new_quantity=order.quantity,
            entry=levels.entry,
            portfolio=portfolio,
            daily=daily,
            max_positions=risk.max_positions,
            max_portfolio_exposure=risk.max_portfolio_exposure_usd,
            max_sector_exposure=risk.max_sector_exposure_usd,
            daily_loss_limit=risk.daily_loss_limit_usd,
            max_daily_trades=risk.max_daily_trades,
        )
        if not risk_gate.passed:
            if risk_gate.details.get("halt_triggered"):
                daily_halted[day_key] = True
            return risk_gate.reason.value

        trading_days = execution.time_horizon_days_map[card.time_horizon]
        return _ExecutionPlan(
            quantity=order.quantity,
            notional=order.notional,
            entry_price=levels.entry,
            stop_price=levels.stop,
            target_price=levels.target,
            time_exit_at=self._calendar.time_exit_at(
                fill_time=_to_utc(entry_bar.start_at),
                trading_days=trading_days,
            ),
        )

    def _atr_20d_before_entry(
        self, card: ExportedThesisCard, t_entry: datetime
    ) -> float | None:
        end = _to_utc(t_entry)
        bars = self._bars.historical_bars(
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            interval="1d",
            start=end - timedelta(days=80),
            end=end,
        )
        entry_date = end.date()
        ordered = sorted(
            (
                bar
                for bar in bars
                if _to_utc(bar.start_at).date() < entry_date
            ),
            key=lambda bar: _to_utc(bar.start_at),
        )
        if len(ordered) < 21:
            return None
        true_ranges: list[float] = []
        for previous, current in zip(ordered[-21:-1], ordered[-20:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges) / 20

    def _target_price(
        self, entry_price: float, direction: str, execution: ExecutionModel
    ) -> float:
        if direction == "buy":
            return entry_price * (1 + execution.take_profit_pct)
        return entry_price * (1 - execution.take_profit_pct)

    def _stop_price(
        self, entry_price: float, direction: str, execution: ExecutionModel
    ) -> float:
        if direction == "buy":
            return entry_price * (1 - execution.stop_loss_pct)
        return entry_price * (1 + execution.stop_loss_pct)

    def _find_exit(
        self,
        *,
        series: BarSeries,
        entry_bar: Bar,
        direction: str,
        execution: ExecutionModel,
        stop: float,
        target: float,
        time_exit_at: datetime,
        ticker_key: str,
        reversal_after: dict[str, list[tuple[datetime, str]]],
    ) -> tuple[Bar, ExitReason]:
        opposing = self._opposing_reversal_at(
            ticker_key, direction, reversal_after, after=_to_utc(entry_bar.start_at)
        )

        last_bar = entry_bar
        for bar in series.iter_after(_to_utc(entry_bar.start_at)):
            last_bar = bar
            bar_at = _to_utc(bar.start_at)

            hit_stop = bar.low <= stop if direction == "buy" else bar.high >= stop
            hit_target = bar.high >= target if direction == "buy" else bar.low <= target

            if hit_stop and hit_target:
                if execution.intrabar_stop_before_target:
                    return bar, ExitReason.STOP_LOSS
                return bar, ExitReason.TAKE_PROFIT
            if hit_stop:
                return bar, ExitReason.STOP_LOSS
            if hit_target:
                return bar, ExitReason.TAKE_PROFIT
            if opposing is not None and bar_at >= opposing:
                return bar, ExitReason.REVERSAL
            if bar_at >= _to_utc(time_exit_at):
                return bar, ExitReason.TIME_STOP

        return last_bar, ExitReason.WINDOW_END

    def _opposing_reversal_at(
        self,
        ticker_key: str,
        direction: str,
        reversal_after: dict[str, list[tuple[datetime, str]]],
        *,
        after: datetime,
    ) -> datetime | None:
        candidates = reversal_after.get(ticker_key, [])
        for t_entry, other_direction in sorted(candidates):
            if t_entry > after and other_direction != direction:
                return t_entry
        return None

    def _build_equity_points(
        self, scenario: str, trades: list[SimulatedTrade]
    ) -> list[EquityPoint]:
        params = self._params
        points: list[EquityPoint] = []
        closed = sorted(
            (t for t in trades if t.is_closed and t.exit_at is not None),
            key=lambda t: (_to_utc(t.exit_at), t.trade_id),
        )
        equity = params.initial_capital
        # Starting point at window start.
        points.append(
            EquityPoint(
                point_id=_point_id(params.run_id, scenario, 0),
                run_id=params.run_id,
                timing_scenario=scenario,
                as_of=_to_utc(params.window_start_at),
                equity=equity,
                open_positions=0,
            )
        )
        for index, trade in enumerate(closed, start=1):
            equity += trade.net_pnl or 0.0
            points.append(
                EquityPoint(
                    point_id=_point_id(params.run_id, scenario, index),
                    run_id=params.run_id,
                    timing_scenario=scenario,
                    as_of=_to_utc(trade.exit_at),
                    equity=equity,
                    open_positions=0,
                )
            )
        # Final point at window end.
        points.append(
            EquityPoint(
                point_id=_point_id(params.run_id, scenario, len(closed) + 1),
                run_id=params.run_id,
                timing_scenario=scenario,
                as_of=_to_utc(params.window_end_at),
                equity=equity,
                open_positions=0,
            )
        )
        return points

    # ----- skipped / blocked trade rows ----------------------------------

    def _skipped_trade(
        self,
        card: ExportedThesisCard,
        scenario: str,
        timing: _CardTiming,
        reason: ExitReason,
        t_entry: datetime,
    ) -> SimulatedTrade:
        return SimulatedTrade(
            trade_id=_trade_id(self._params.run_id, card.id, scenario),
            run_id=self._params.run_id,
            thesis_card_id=card.id,
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            strategy=card.strategy,
            direction=card.direction,
            card_decision_state=_decision_state(card),
            card_was_live_expired=self._was_live_expired(card, t_entry),
            entry_timing_scenario=scenario,
            news_published_at=timing.news_published_at,
            news_fetched_at=timing.news_fetched_at,
            card_created_at=timing.t_actual,
            news_fetch_delay_seconds=timing.news_fetch_delay_seconds,
            thesis_build_delay_seconds=timing.thesis_build_delay_seconds,
            total_pipeline_delay_seconds=timing.total_pipeline_delay_seconds,
            entry_at=None,
            entry_price=None,
            quantity=None,
            exit_at=None,
            exit_price=None,
            gross_pnl=None,
            commission=None,
            slippage=None,
            net_pnl=None,
            return_pct=None,
            exit_reason=reason,
            risk_block_rule=None,
            holding_period_seconds=None,
        )

    def _risk_blocked_trade(
        self,
        card: ExportedThesisCard,
        scenario: str,
        timing: _CardTiming,
        rule: str,
    ) -> SimulatedTrade:
        t_entry = self._entry_time(scenario, timing)
        return SimulatedTrade(
            trade_id=_trade_id(self._params.run_id, card.id, scenario),
            run_id=self._params.run_id,
            thesis_card_id=card.id,
            ticker=card.ticker,
            exchange_code=card.exchange_code,
            strategy=card.strategy,
            direction=card.direction,
            card_decision_state=_decision_state(card),
            card_was_live_expired=self._was_live_expired(card, t_entry),
            entry_timing_scenario=scenario,
            news_published_at=timing.news_published_at,
            news_fetched_at=timing.news_fetched_at,
            card_created_at=timing.t_actual,
            news_fetch_delay_seconds=timing.news_fetch_delay_seconds,
            thesis_build_delay_seconds=timing.thesis_build_delay_seconds,
            total_pipeline_delay_seconds=timing.total_pipeline_delay_seconds,
            entry_at=None,
            entry_price=None,
            quantity=None,
            exit_at=None,
            exit_price=None,
            gross_pnl=None,
            commission=None,
            slippage=None,
            net_pnl=None,
            return_pct=None,
            exit_reason=ExitReason.RISK_BLOCKED,
            risk_block_rule=rule,
            holding_period_seconds=None,
        )

    # ----- metrics --------------------------------------------------------

    def _build_metrics(
        self,
        *,
        cards_considered: int,
        cards_in_population: int,
        cards_live_executable: int,
        cards_skipped_no_price: int,
        scenarios: list[str],
        population: list[ExportedThesisCard],
        per_scenario_trades: dict[str, list[SimulatedTrade]],
        per_scenario_equity: dict[str, list[EquityPoint]],
    ) -> RunMetrics:
        primary = scenarios[0]
        primary_trades = per_scenario_trades[primary]
        primary_equity = per_scenario_equity[primary]

        agg = _aggregate_trades(primary_trades)
        net_pnl = agg.net_pnl
        total_return = (
            net_pnl / self._params.initial_capital
            if self._params.initial_capital
            else None
        )

        equity_values = [point.equity for point in primary_equity]
        mdd = max_drawdown(equity_values)
        _mdd_frac, mdd_duration = max_drawdown_with_duration(
            [(point.as_of, point.equity) for point in primary_equity]
        )
        period_returns = _period_returns(equity_values)
        sharpe = sharpe_ratio(period_returns, self._params.risk_free_rate_per_period)

        delays = _delay_aggregates(population, self._card_timing)
        exposure = _exposure_fraction(
            primary_trades, self._params.window_start_at, self._params.window_end_at
        )
        signal_accuracy = _signal_accuracy(primary_trades)

        pnl_gap = None
        win_rate_gap = None
        trades_flipped = None
        if self._params.timing_scenario == TimingScenario.BOTH:
            ideal_agg = _aggregate_trades(per_scenario_trades[TimingScenario.IDEAL.value])
            actual_agg = _aggregate_trades(
                per_scenario_trades[TimingScenario.ACTUAL.value]
            )
            pnl_gap = ideal_agg.net_pnl - actual_agg.net_pnl
            iw = ideal_agg.win_rate
            aw = actual_agg.win_rate
            if iw is not None and aw is not None:
                win_rate_gap = round_half_up(iw - aw, 5)
            trades_flipped = _trades_flipped(
                per_scenario_trades[TimingScenario.IDEAL.value],
                per_scenario_trades[TimingScenario.ACTUAL.value],
            )

        summary_json = _build_summary_json(
            scenarios=scenarios,
            per_scenario_trades=per_scenario_trades,
            profit_factor_undefined=(agg.gross_loss == 0 and agg.gross_profit > 0),
        )

        return RunMetrics(
            cards_considered=cards_considered,
            cards_in_population=cards_in_population,
            cards_live_executable=cards_live_executable,
            cards_skipped_no_price=cards_skipped_no_price,
            trades_opened=agg.opened,
            trades_closed=agg.closed,
            trades_risk_blocked=agg.risk_blocked,
            net_pnl=net_pnl,
            gross_profit=agg.gross_profit,
            gross_loss=agg.gross_loss,
            total_commission=agg.total_commission,
            total_slippage=agg.total_slippage,
            total_return=_round5(total_return),
            win_rate=_round5(agg.win_rate),
            avg_win=agg.avg_win,
            avg_loss=agg.avg_loss,
            profit_factor=_round5(agg.profit_factor),
            expectancy=agg.expectancy,
            max_drawdown=_round5(mdd),
            max_drawdown_duration_seconds=mdd_duration,
            sharpe_ratio=_round5(sharpe),
            exposure_fraction=_round5(exposure),
            signal_accuracy=_round5(signal_accuracy),
            avg_news_fetch_delay_seconds=delays["news_fetch"]["avg"],
            p95_news_fetch_delay_seconds=delays["news_fetch"]["p95"],
            max_news_fetch_delay_seconds=delays["news_fetch"]["max"],
            avg_thesis_build_delay_seconds=delays["thesis_build"]["avg"],
            p95_thesis_build_delay_seconds=delays["thesis_build"]["p95"],
            max_thesis_build_delay_seconds=delays["thesis_build"]["max"],
            avg_total_pipeline_delay_seconds=delays["total_pipeline"]["avg"],
            p95_total_pipeline_delay_seconds=delays["total_pipeline"]["p95"],
            max_total_pipeline_delay_seconds=delays["total_pipeline"]["max"],
            pnl_gap=pnl_gap,
            win_rate_gap=win_rate_gap,
            trades_flipped_by_delay=trades_flipped,
            summary_json=summary_json,
            summary_md=_build_summary_md(agg, net_pnl),
        )


# ----- aggregation helpers ----------------------------------------------


@dataclass(frozen=True)
class _TradeAgg:
    opened: int
    closed: int
    risk_blocked: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    total_commission: float
    total_slippage: float
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    expectancy: float | None


def _aggregate_trades(trades: list[SimulatedTrade]) -> _TradeAgg:
    closed = [t for t in trades if t.is_closed]
    opened = sum(1 for t in trades if t.entry_at is not None)
    risk_blocked = sum(1 for t in trades if t.exit_reason == ExitReason.RISK_BLOCKED)
    wins = [t for t in closed if (t.net_pnl or 0.0) > 0]
    losses = [t for t in closed if (t.net_pnl or 0.0) < 0]
    gross_profit = sum(t.net_pnl or 0.0 for t in wins)
    gross_loss = abs(sum(t.net_pnl or 0.0 for t in losses))
    net_pnl = sum(t.net_pnl or 0.0 for t in closed)
    total_commission = sum(t.commission or 0.0 for t in closed)
    total_slippage = sum(t.slippage or 0.0 for t in closed)
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (-gross_loss / len(losses)) if losses else None
    return _TradeAgg(
        opened=opened,
        closed=len(closed),
        risk_blocked=risk_blocked,
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_commission=total_commission,
        total_slippage=total_slippage,
        win_rate=win_rate(len(wins), len(closed)),
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor(gross_profit, gross_loss),
        expectancy=expectancy(net_pnl, len(closed)),
    )


def _period_returns(equity_values: list[float]) -> list[float]:
    returns: list[float] = []
    for prev, curr in zip(equity_values, equity_values[1:]):
        if prev:
            returns.append((curr - prev) / prev)
    return returns


def _delay_aggregates(population, card_timing) -> dict[str, dict[str, float | None]]:
    fetch: list[float] = []
    thesis: list[float] = []
    total: list[float] = []
    for card in population:
        timing = card_timing(card)
        if timing.news_fetch_delay_seconds is not None:
            fetch.append(timing.news_fetch_delay_seconds)
        if timing.thesis_build_delay_seconds is not None:
            thesis.append(timing.thesis_build_delay_seconds)
        if timing.total_pipeline_delay_seconds is not None:
            total.append(timing.total_pipeline_delay_seconds)
    return {
        "news_fetch": _delay_summary(fetch),
        "thesis_build": _delay_summary(thesis),
        "total_pipeline": _delay_summary(total),
    }


def _delay_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "p95": None, "max": None}
    return {
        "avg": sum(values) / len(values),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def _exposure_fraction(
    trades: list[SimulatedTrade], window_start: datetime, window_end: datetime
) -> float | None:
    window_seconds = (_to_utc(window_end) - _to_utc(window_start)).total_seconds()
    if window_seconds <= 0:
        return None
    in_market = sum(
        t.holding_period_seconds or 0.0
        for t in trades
        if t.is_closed and t.holding_period_seconds is not None
    )
    fraction = in_market / window_seconds
    return min(1.0, fraction)


def _signal_accuracy(trades: list[SimulatedTrade]) -> float | None:
    closed = [t for t in trades if t.is_closed]
    if not closed:
        return None
    correct = sum(1 for t in closed if (t.net_pnl or 0.0) > 0)
    return correct / len(closed)


def _trades_flipped(
    ideal: list[SimulatedTrade], actual: list[SimulatedTrade]
) -> int:
    actual_by_card = {t.thesis_card_id: t for t in actual}
    flipped = 0
    for ideal_trade in ideal:
        actual_trade = actual_by_card.get(ideal_trade.thesis_card_id)
        if actual_trade is None:
            continue
        ideal_filled = ideal_trade.is_closed
        actual_filled = actual_trade.is_closed
        if ideal_filled != actual_filled:
            flipped += 1
            continue
        if not ideal_filled:
            continue
        ideal_win = (ideal_trade.net_pnl or 0.0) > 0
        actual_win = (actual_trade.net_pnl or 0.0) > 0
        if ideal_win != actual_win:
            flipped += 1
    return flipped


def _build_summary_json(
    *,
    scenarios: list[str],
    per_scenario_trades: dict[str, list[SimulatedTrade]],
    profit_factor_undefined: bool,
) -> dict:
    primary = scenarios[0]
    trades = per_scenario_trades[primary]

    by_strategy: dict[str, dict] = {}
    strategies = sorted({t.strategy for t in trades})
    for strategy in strategies:
        subset = [t for t in trades if t.strategy == strategy]
        by_strategy[strategy] = _agg_to_dict(_aggregate_trades(subset))

    by_card_status: dict[str, dict] = {}
    for state in ("approved", "rejected"):
        subset = [t for t in trades if t.card_decision_state == state]
        by_card_status[state] = _agg_to_dict(_aggregate_trades(subset))
    live_expired = [t for t in trades if t.card_was_live_expired]
    by_card_status["card_was_live_expired"] = _agg_to_dict(_aggregate_trades(live_expired))
    not_expired = [t for t in trades if not t.card_was_live_expired]
    by_card_status["card_unexpired_at_entry"] = _agg_to_dict(_aggregate_trades(not_expired))

    return {
        "profit_factor_undefined": profit_factor_undefined,
        "by_strategy": by_strategy,
        "by_card_status": by_card_status,
    }


def _agg_to_dict(agg: _TradeAgg) -> dict:
    return {
        "trades_opened": agg.opened,
        "trades_closed": agg.closed,
        "trades_risk_blocked": agg.risk_blocked,
        "net_pnl": agg.net_pnl,
        "gross_profit": agg.gross_profit,
        "gross_loss": agg.gross_loss,
        "win_rate": _round5(agg.win_rate),
        "avg_win": agg.avg_win,
        "avg_loss": agg.avg_loss,
        "profit_factor": _round5(agg.profit_factor),
        "expectancy": agg.expectancy,
    }


def _build_summary_md(agg: _TradeAgg, net_pnl: float) -> str:
    win = "n/a" if agg.win_rate is None else f"{agg.win_rate:.2%}"
    pf = "n/a" if agg.profit_factor is None else f"{agg.profit_factor:.2f}"
    return (
        f"Backtest summary: {agg.closed} closed trades, net P&L {net_pnl:.2f}, "
        f"win rate {win}, profit factor {pf}."
    )


def _round5(value: float | None) -> float | None:
    if value is None:
        return None
    return round_half_up(value, 5)


def _snapshot_id(run_id: str, card_id: str) -> str:
    return f"bts_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{card_id}').hex}"


def _trade_id(run_id: str, card_id: str, scenario: str) -> str:
    return f"btt_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{card_id}:{scenario}').hex}"


def _point_id(run_id: str, scenario: str, index: int) -> str:
    return f"bte_{uuid.uuid5(uuid.NAMESPACE_URL, f'{run_id}:{scenario}:{index}').hex}"
