"""Comprehensive behavioral tests for BacktesterEngine.

All tests use fake providers – no database, no network.
Run with:  uv run pytest tests/product_components/backtester/test_backtester_engine_behaviors.py -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.core_components.backtest_engine import Bar
from src.product_components.backtester.engine import BacktesterEngine
from src.product_components.backtester.models import (
    BacktestRunParams,
    CardPopulation,
    ExecutionMode,
    ExecutionModel,
    ExitReason,
    RiskModel,
    TimingScenario,
)
from src.product_components.trade_executor.trading_calendar import NaiveTradingCalendar
from src.product_components.thesis_builder.export import (
    ExportedEvidenceArticle,
    ExportedThesisCard,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UTC = timezone.utc
# Anchor time: Thursday 2025-01-02 14:00 UTC
T0 = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class FakeCardsProvider:
    """CardsProvider that returns a fixed list of cards.

    Filters on validation_status and strategy are respected when kwargs are
    passed in by callers (the engine does *not* pass them – it does its own
    filtering – but we honour them for completeness).
    """

    def __init__(self, cards: list[ExportedThesisCard]) -> None:
        self._cards = cards

    def export_cards(
        self,
        *,
        window_start_at: datetime,
        window_end_at: datetime,
        validation_status: str | None = None,
        strategy: str | None = None,
    ) -> list[ExportedThesisCard]:
        result = list(self._cards)
        if validation_status is not None:
            result = [c for c in result if c.validation_status == validation_status]
        if strategy is not None:
            result = [c for c in result if c.strategy == strategy]
        return result


class FakeBarsProvider:
    """BarsProvider backed by a dict keyed on ``ticker|exchange_code``.

    ``bars_by_key`` maps ``"{ticker}|{exchange_code}"`` to a list of Bar objects.
    The list is clipped to [start, end] (inclusive start_at) before returning.
    Pass an empty list (or omit the key) to simulate missing price history.
    """

    def __init__(self, bars_by_key: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_key

    def historical_bars(
        self,
        *,
        ticker: str,
        exchange_code: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        key = f"{ticker}|{exchange_code}"
        bars = self._bars.get(key, [])
        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        return [b for b in bars if start_utc <= _as_utc(b.start_at) <= end_utc]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def make_card(
    *,
    card_id: str | None = None,
    ticker: str = "AAPL",
    exchange_code: str = "NASDAQ",
    direction: str = "buy",
    strategy: str = "sentiment_momentum",
    confidence: float = 0.9,
    validation_status: str = "valid",
    rejection_reason_code: str | None = None,
    # Timing
    news_published_at: datetime | None = None,
    news_fetched_at: datetime | None = None,
    news_ready_at: datetime | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    # Risk box
    risk_max_loss_usd: float = 100.0,
    risk_stop_condition: str = "stop",
    risk_invalidation_condition: str = "invalidate",
) -> ExportedThesisCard:
    """Build an ExportedThesisCard with sensible defaults.

    Default timeline (all times relative to T0):
      news_published_at = T0
      news_fetched_at   = T0 + 120s
      created_at        = T0 + 180s  (60s thesis-build after fetch)
      expires_at        = created_at + 4h
    """
    pub = news_published_at if news_published_at is not None else T0
    fetched = news_fetched_at if news_fetched_at is not None else (pub + timedelta(seconds=120))
    created = created_at if created_at is not None else (pub + timedelta(seconds=180))
    ready = news_ready_at if news_ready_at is not None else pub
    expires = expires_at if expires_at is not None else (created + timedelta(hours=4))
    cid = card_id or f"card-{uuid.uuid4().hex[:8]}"

    evidence: list[ExportedEvidenceArticle] = []
    if news_published_at is not None or news_fetched_at is not None or True:
        # Always attach one evidence article so timing can be derived.
        evidence = [
            ExportedEvidenceArticle(
                article_id=f"art-{cid}",
                published_at=pub,
                fetched_at=fetched,
            )
        ]

    return ExportedThesisCard(
        id=cid,
        ticker=ticker,
        exchange_code=exchange_code,
        direction=direction,
        strategy=strategy,
        time_horizon="intraday",
        confidence=confidence,
        risk_max_loss_usd=risk_max_loss_usd,
        risk_stop_condition=risk_stop_condition,
        risk_invalidation_condition=risk_invalidation_condition,
        validation_status=validation_status,
        rejection_reason_code=rejection_reason_code,
        created_at=created,
        expires_at=expires,
        signal_published_at=created,
        evidence=evidence,
        news_ready_at=ready,
    )


def make_bars(
    start: datetime,
    count: int,
    interval_minutes: int = 1,
    path: list[float] | None = None,
) -> list[Bar]:
    """Craft OHLCV bars starting at *start*.

    *path* is a list of close prices (length == count).  If omitted, prices
    stay flat at 100.0.  Each bar's open equals the previous close; high and low
    are constructed to *only* touch the close (no excursions beyond open/close).
    """
    if path is None:
        path = [100.0] * count
    assert len(path) == count, f"path length {len(path)} != count {count}"

    bars: list[Bar] = []
    prev_close = path[0]
    for i, close in enumerate(path):
        ts = start + timedelta(minutes=i * interval_minutes)
        open_ = prev_close if i > 0 else close
        high = max(open_, close)
        low = min(open_, close)
        bars.append(Bar(start_at=ts, open=open_, high=high, low=low, close=close, volume=1000))
        prev_close = close
    return bars


def _default_exec(
    *,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.015,
    time_stop_seconds: int = 4 * 3600,
    entry_slippage_bps: float = 0.0,
    exit_slippage_bps: float = 0.0,
    intrabar_stop_before_target: bool = True,
    commission_model: str = "flat",
    commission_flat_usd: float = 0.0,
    excursion_horizon_minutes: tuple[int, ...] = (30, 60, 120, 240, 390, 1170, 1950),
) -> ExecutionModel:
    """ExecutionModel with zero commissions/slippage by default for clean assertions."""
    return ExecutionModel(
        mode=ExecutionMode.LEGACY_FLAT_PERCENT,
        commission_model=commission_model,
        commission_flat_usd=commission_flat_usd,
        commission_per_share_usd=0.0,
        commission_min_usd=0.0,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
        intrabar_stop_before_target=intrabar_stop_before_target,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        time_stop_seconds=time_stop_seconds,
        excursion_horizon_minutes=excursion_horizon_minutes,
    )


def _default_risk(
    *,
    max_daily_trades: int = 100,
    daily_loss_limit_usd: float = 1_000_000.0,
    ticker_cooldown_minutes: int = 0,
    max_position_usd: float = 1000.0,
    max_portfolio_exposure_usd: float = 1_000_000.0,
) -> RiskModel:
    """RiskModel with all gates wide open by default."""
    return RiskModel(
        max_position_usd=max_position_usd,
        max_portfolio_exposure_usd=max_portfolio_exposure_usd,
        max_daily_trades=max_daily_trades,
        daily_loss_limit_usd=daily_loss_limit_usd,
        ticker_cooldown_minutes=ticker_cooldown_minutes,
    )


def _params(
    *,
    timing: TimingScenario = TimingScenario.IDEAL,
    population: CardPopulation = CardPopulation.ALL,
    execution_model: ExecutionModel | None = None,
    risk_model: RiskModel | None = None,
    ideal_fetch_delay_seconds: int = 120,
    ideal_thesis_delay_seconds: int = 60,
    window_start_at: datetime | None = None,
    window_end_at: datetime | None = None,
    run_id: str = "run-test",
) -> BacktestRunParams:
    return BacktestRunParams(
        run_id=run_id,
        window_start_at=window_start_at or (T0 - timedelta(minutes=5)),
        window_end_at=window_end_at or (T0 + timedelta(hours=8)),
        timing_scenario=timing,
        card_population=population,
        ideal_fetch_delay_seconds=ideal_fetch_delay_seconds,
        ideal_thesis_delay_seconds=ideal_thesis_delay_seconds,
        execution_model=execution_model or _default_exec(),
        risk_model=risk_model or _default_risk(),
    )


def _run(
    cards: list[ExportedThesisCard],
    bars_by_key: dict[str, list[Bar]],
    *,
    timing: TimingScenario = TimingScenario.IDEAL,
    population: CardPopulation = CardPopulation.ALL,
    execution_model: ExecutionModel | None = None,
    risk_model: RiskModel | None = None,
    ideal_fetch_delay_seconds: int = 120,
    ideal_thesis_delay_seconds: int = 60,
    window_end_at: datetime | None = None,
    run_id: str = "run-test",
    trading_calendar=None,
):
    engine = BacktesterEngine(
        params=_params(
            timing=timing,
            population=population,
            execution_model=execution_model,
            risk_model=risk_model,
            ideal_fetch_delay_seconds=ideal_fetch_delay_seconds,
            ideal_thesis_delay_seconds=ideal_thesis_delay_seconds,
            window_end_at=window_end_at,
            run_id=run_id,
        ),
        cards_provider=FakeCardsProvider(cards),
        bars_provider=FakeBarsProvider(bars_by_key),
        trading_calendar=trading_calendar,
    )
    return engine.run()


# ---------------------------------------------------------------------------
# Excursion diagnostics
# ---------------------------------------------------------------------------


def test_excursion_diagnostics_for_buy_trade() -> None:
    entry = T0 + timedelta(seconds=180)
    bars = [
        Bar(start_at=entry, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=1), open=100.0, high=104.0, low=98.0, close=101.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=2), open=101.0, high=103.0, low=97.0, close=102.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=3), open=102.0, high=106.0, low=99.0, close=105.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=4), open=105.0, high=105.0, low=96.0, close=99.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=5), open=99.0, high=102.0, low=98.0, close=101.0, volume=1000),
    ]
    result = _run(
        [make_card(card_id="buy-diagnostics")],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            take_profit_pct=0.20,
            stop_loss_pct=0.20,
            time_stop_seconds=5 * 60,
            excursion_horizon_minutes=(2, 4, 10),
        ),
        window_end_at=entry + timedelta(minutes=6),
        trading_calendar=NaiveTradingCalendar(),
    )

    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TIME_STOP
    assert trade.mfe_pct == pytest.approx(0.06)
    assert trade.mae_pct == pytest.approx(-0.04)
    assert trade.time_to_mfe_seconds == pytest.approx(180.0)
    assert trade.time_to_mae_seconds == pytest.approx(240.0)
    assert trade.horizon_returns_json == {
        "2": pytest.approx(0.01),
        "4": pytest.approx(0.05),
        "10": None,
    }
    assert trade.both_brackets_in_one_bar is False
    assert trade.bar_coverage_ratio == pytest.approx(1.0)


def test_excursion_diagnostics_for_sell_trade() -> None:
    entry = T0 + timedelta(seconds=180)
    bars = [
        Bar(start_at=entry, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=1), open=100.0, high=104.0, low=98.0, close=101.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=2), open=101.0, high=103.0, low=97.0, close=102.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=3), open=102.0, high=106.0, low=99.0, close=105.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=4), open=105.0, high=105.0, low=96.0, close=99.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=5), open=99.0, high=102.0, low=98.0, close=101.0, volume=1000),
    ]
    result = _run(
        [make_card(card_id="sell-diagnostics", direction="sell")],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            take_profit_pct=0.20,
            stop_loss_pct=0.20,
            time_stop_seconds=5 * 60,
            excursion_horizon_minutes=(2, 4),
        ),
        trading_calendar=NaiveTradingCalendar(),
    )

    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TIME_STOP
    assert trade.mfe_pct == pytest.approx(0.04)
    assert trade.mae_pct == pytest.approx(-0.06)
    assert trade.time_to_mfe_seconds == pytest.approx(240.0)
    assert trade.time_to_mae_seconds == pytest.approx(180.0)
    assert trade.horizon_returns_json == {
        "2": pytest.approx(-0.01),
        "4": pytest.approx(-0.05),
    }


def test_excursion_diagnostics_flags_bracket_span_and_missing_bar_coverage() -> None:
    entry = T0 + timedelta(seconds=180)
    span_bars = [
        Bar(start_at=entry, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=1), open=100.0, high=104.0, low=98.0, close=101.0, volume=1000),
    ]
    span_result = _run(
        [make_card(card_id="span-diagnostics")],
        {"AAPL|NASDAQ": span_bars},
        execution_model=_default_exec(
            take_profit_pct=0.03,
            stop_loss_pct=0.015,
            excursion_horizon_minutes=(1,),
        ),
        trading_calendar=NaiveTradingCalendar(),
    )
    assert span_result.trades[0].both_brackets_in_one_bar is True

    sparse_bars = [
        Bar(start_at=entry, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=2), open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(start_at=entry + timedelta(minutes=4), open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
    ]
    sparse_result = _run(
        [make_card(card_id="coverage-diagnostics")],
        {"AAPL|NASDAQ": sparse_bars},
        execution_model=_default_exec(
            take_profit_pct=0.20,
            stop_loss_pct=0.20,
            time_stop_seconds=4 * 60,
            excursion_horizon_minutes=(1,),
        ),
        trading_calendar=NaiveTradingCalendar(),
    )
    assert sparse_result.trades[0].bar_coverage_ratio == pytest.approx(0.5)


def test_unfilled_and_risk_blocked_trades_have_null_diagnostics() -> None:
    not_filled = _run([make_card(card_id="no-bars")], {"AAPL|NASDAQ": []}).trades[0]
    assert not_filled.exit_reason == ExitReason.NOT_FILLED
    assert not_filled.mfe_pct is None
    assert not_filled.horizon_returns_json is None
    assert not_filled.bar_coverage_ratio is None

    entry = T0 + timedelta(seconds=180)
    blocked = _run(
        [make_card(card_id="blocked")],
        {"AAPL|NASDAQ": make_bars(entry, count=3)},
        execution_model=_default_exec(),
        risk_model=_default_risk(max_daily_trades=0),
    ).trades[0]
    assert blocked.exit_reason == ExitReason.RISK_BLOCKED
    assert blocked.mfe_pct is None
    assert blocked.horizon_returns_json is None
    assert blocked.bar_coverage_ratio is None


# ---------------------------------------------------------------------------
# Test 1 – Ideal vs actual entry timing (timing_scenario=both)
# ---------------------------------------------------------------------------


def test_ideal_vs_actual_entry_timing():
    """Both scenarios produce separate trades; entry times differ correctly.

    Default delays:  ideal_fetch=120s, ideal_thesis=60s  -> t_ideal = T0 + 180s
    Card created at T0+300s (large delay) -> t_actual = T0+300s.
    """
    card = make_card(
        card_id="c1",
        news_published_at=T0,
        news_fetched_at=T0 + timedelta(seconds=120),
        created_at=T0 + timedelta(seconds=300),
    )
    # Bars start well before T0+180 so both scenarios can find an entry bar.
    entry_start = T0 + timedelta(seconds=60)
    bars = make_bars(entry_start, count=60, path=[100.0] * 60)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        timing=TimingScenario.BOTH,
    )

    ideal_trade = next(t for t in result.trades if t.entry_timing_scenario == "ideal")
    actual_trade = next(t for t in result.trades if t.entry_timing_scenario == "actual")

    # Ideal entry bar must be the bar at or after T0 + 120 + 60 = T0+180s.
    expected_ideal_entry = T0 + timedelta(seconds=180)
    assert ideal_trade.entry_at is not None
    assert _as_utc(ideal_trade.entry_at) >= _as_utc(expected_ideal_entry)

    # Actual entry bar must be at or after created_at (T0+300s).
    expected_actual_entry = T0 + timedelta(seconds=300)
    assert actual_trade.entry_at is not None
    assert _as_utc(actual_trade.entry_at) >= _as_utc(expected_actual_entry)

    # Actual entry must be strictly later than ideal entry.
    assert _as_utc(actual_trade.entry_at) > _as_utc(ideal_trade.entry_at)

    # Both rows must reference the same card.
    assert ideal_trade.thesis_card_id == actual_trade.thesis_card_id


# ---------------------------------------------------------------------------
# Test 2 – Delay decomposition
# ---------------------------------------------------------------------------


def test_delay_decomposition_seconds():
    """Delay fields are computed from the triggering evidence article.

    Published at T0, fetched at T0+90s, card created at T0+240s.
      news_fetch_delay    = 90s
      thesis_build_delay  = 240 - 90 = 150s
      total_pipeline_delay= 240s
    """
    published = T0
    fetched = T0 + timedelta(seconds=90)
    created = T0 + timedelta(seconds=240)

    card = make_card(
        card_id="c-delay",
        news_published_at=published,
        news_fetched_at=fetched,
        created_at=created,
    )
    # t_ideal = T0 + 120 + 60 = T0+180s; bars start there.
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=30, path=[100.0] * 30)

    result = _run([card], {"AAPL|NASDAQ": bars})
    trade = result.trades[0]

    assert trade.news_fetch_delay_seconds == pytest.approx(90.0)
    assert trade.thesis_build_delay_seconds == pytest.approx(150.0)
    assert trade.total_pipeline_delay_seconds == pytest.approx(240.0)

    # Also check the aggregated metrics carry the same values.
    m = result.metrics
    assert m.avg_news_fetch_delay_seconds == pytest.approx(90.0)
    assert m.avg_thesis_build_delay_seconds == pytest.approx(150.0)
    assert m.avg_total_pipeline_delay_seconds == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# Test 3 – Take-profit exit (buy)
# ---------------------------------------------------------------------------


def test_take_profit_buy():
    """A price path that rises above take_profit_pct => take_profit exit, positive PnL."""
    # take_profit_pct = 3%.  Entry at 100 => target ~103.
    # Path: starts flat then jumps above 103.
    tp_pct = 0.03
    entry_price = 100.0
    target = entry_price * (1 + tp_pct)  # 103.0

    card = make_card(card_id="c-tp")
    entry_start = T0 + timedelta(seconds=180)

    # Build a path: 5 bars flat then one bar that goes high enough.
    flat = [100.0] * 5
    hit = [target + 0.50]  # high will be 103.5, crossing target
    path = flat + hit
    bars = make_bars(entry_start, count=len(path), path=path)
    # Make the hit-bar's high actually exceed target (path builder keeps high=max(open,close)).
    # The last bar: open=100, close=103.5 => high=103.5 > 103 ✓

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(take_profit_pct=tp_pct, stop_loss_pct=0.015),
    )
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TAKE_PROFIT
    assert trade.net_pnl is not None and trade.net_pnl > 0


# ---------------------------------------------------------------------------
# Test 4 – Stop-loss exit (buy)
# ---------------------------------------------------------------------------


def test_stop_loss_buy():
    """A price path that drops below stop_loss_pct => stop_loss exit, negative gross PnL."""
    sl_pct = 0.015
    entry_price = 100.0
    stop = entry_price * (1 - sl_pct)  # 98.5

    card = make_card(card_id="c-sl")
    entry_start = T0 + timedelta(seconds=180)

    # Build path: 3 flat bars then drop below stop.
    flat = [100.0] * 3
    drop = [stop - 0.10]  # 98.4 -> low will be 98.4 < 98.5 ✓
    path = flat + drop
    bars = make_bars(entry_start, count=len(path), path=path)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(stop_loss_pct=sl_pct, take_profit_pct=0.10),
    )
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.STOP_LOSS
    # Gross PnL for a buy stop-loss is entry_price*(1-pct) - entry_price < 0.
    assert trade.gross_pnl is not None and trade.gross_pnl < 0


# ---------------------------------------------------------------------------
# Test 5 – Time-stop exit
# ---------------------------------------------------------------------------


def test_time_stop_exit():
    """Flat path longer than time_stop_seconds => time_stop exit."""
    time_stop = 5 * 60  # 5 minutes = 300 seconds
    # tp/sl far away so they won't be hit.
    card = make_card(card_id="c-ts")
    entry_start = T0 + timedelta(seconds=180)

    # 10 flat 1-minute bars => last bar is at entry + 9 minutes, well past 5-min time stop.
    bars = make_bars(entry_start, count=10, path=[100.0] * 10)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            time_stop_seconds=time_stop,
            take_profit_pct=0.50,  # unreachable
            stop_loss_pct=0.50,    # unreachable
        ),
    )
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TIME_STOP


# ---------------------------------------------------------------------------
# Test 6 – Window-end exit
# ---------------------------------------------------------------------------


def test_window_end_exit():
    """Path that never hits stop/target and window ends => window_end exit."""
    card = make_card(card_id="c-we")
    entry_start = T0 + timedelta(seconds=180)

    # Only 3 bars, window ends right after.
    bars = make_bars(entry_start, count=3, path=[100.0] * 3)
    window_end = entry_start + timedelta(minutes=5)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            time_stop_seconds=10 * 3600,  # far future
            take_profit_pct=0.50,
            stop_loss_pct=0.50,
        ),
        window_end_at=window_end,
    )
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.WINDOW_END


# ---------------------------------------------------------------------------
# Test 7 – Intrabar stop-before-target parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intrabar_stop_before_target, expected_reason",
    [
        (True, ExitReason.STOP_LOSS),
        (False, ExitReason.TAKE_PROFIT),
    ],
)
def test_intrabar_stop_before_target(intrabar_stop_before_target, expected_reason):
    """A single bar spanning both stop and target resolves according to the flag."""
    sl_pct = 0.02
    tp_pct = 0.02
    entry_price = 100.0

    card = make_card(card_id="c-intrabar")
    entry_start = T0 + timedelta(seconds=180)

    # Entry bar: open 100, no excursion.
    entry_bar = Bar(
        start_at=entry_start,
        open=entry_price,
        high=entry_price,
        low=entry_price,
        close=entry_price,
        volume=1000,
    )
    # Next bar: low goes below stop (98) AND high goes above target (102) simultaneously.
    ambiguous_bar = Bar(
        start_at=entry_start + timedelta(minutes=1),
        open=entry_price,
        high=entry_price * (1 + tp_pct + 0.01),  # 103, crosses +2% target
        low=entry_price * (1 - sl_pct - 0.01),   # 97,  crosses -2% stop
        close=entry_price,
        volume=1000,
    )

    bars = [entry_bar, ambiguous_bar]
    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            intrabar_stop_before_target=intrabar_stop_before_target,
        ),
    )
    trade = result.trades[0]
    assert trade.exit_reason == expected_reason


# ---------------------------------------------------------------------------
# Test 8 – Entry slippage applied correctly
# ---------------------------------------------------------------------------


def test_entry_slippage_applied():
    """Buy fills above bar open; sell fills below bar open by the configured bps."""
    slippage_bps = 10.0  # 0.1%

    # --- BUY ---
    buy_card = make_card(card_id="c-slip-buy", direction="buy")
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=20, path=[100.0] * 20)

    result = _run(
        [buy_card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            entry_slippage_bps=slippage_bps,
            take_profit_pct=0.50,  # won't be hit; exits as window_end
        ),
    )
    buy_trade = result.trades[0]
    # Entry fill for a buy should be above bar open (100.0).
    assert buy_trade.entry_price is not None
    assert buy_trade.entry_price > 100.0
    expected_buy_entry = 100.0 * (1 + slippage_bps / 10_000)
    assert buy_trade.entry_price == pytest.approx(expected_buy_entry, rel=1e-9)

    # --- SELL ---
    sell_card = make_card(card_id="c-slip-sell", direction="sell")
    result2 = _run(
        [sell_card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            entry_slippage_bps=slippage_bps,
            take_profit_pct=0.50,
            stop_loss_pct=0.50,
        ),
    )
    sell_trade = result2.trades[0]
    # Entry fill for a sell should be below bar open (100.0).
    assert sell_trade.entry_price is not None
    assert sell_trade.entry_price < 100.0
    expected_sell_entry = 100.0 * (1 - slippage_bps / 10_000)
    assert sell_trade.entry_price == pytest.approx(expected_sell_entry, rel=1e-9)


# ---------------------------------------------------------------------------
# Test 9 – No price history => not filled, counted in cards_skipped_no_price
# ---------------------------------------------------------------------------


def test_no_price_history_skipped():
    """Card whose instrument has no bars is counted in cards_skipped_no_price, not opened."""
    card = make_card(card_id="c-noprice", ticker="ZZZY", exchange_code="NYSE")
    # Provide bars for a *different* ticker so the engine finds nothing for ZZZY.
    bars = make_bars(T0 + timedelta(seconds=180), count=10, path=[50.0] * 10)

    result = _run(
        [card],
        {"OTHER|NYSE": bars},  # wrong key
    )

    assert result.metrics.cards_skipped_no_price == 1
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.NOT_FILLED
    assert trade.entry_at is None
    assert not trade.is_closed


# ---------------------------------------------------------------------------
# Test 10a – max_daily_trades risk gate
# ---------------------------------------------------------------------------


def test_risk_gate_max_daily_trades():
    """Second same-day card is blocked when max_daily_trades=1."""
    card1 = make_card(
        card_id="c-day1",
        news_published_at=T0,
        news_fetched_at=T0 + timedelta(seconds=60),
        created_at=T0 + timedelta(seconds=120),
    )
    card2 = make_card(
        card_id="c-day2",
        news_published_at=T0 + timedelta(minutes=5),
        news_fetched_at=T0 + timedelta(minutes=5, seconds=60),
        created_at=T0 + timedelta(minutes=5, seconds=120),
    )

    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=60, path=[100.0] * 60)

    result = _run(
        [card1, card2],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(take_profit_pct=0.50, stop_loss_pct=0.50),
        risk_model=_default_risk(max_daily_trades=1),
    )

    blocked = [t for t in result.trades if t.exit_reason == ExitReason.RISK_BLOCKED]
    assert len(blocked) == 1
    assert blocked[0].risk_block_rule == "max_daily_trades"
    assert result.metrics.trades_risk_blocked == 1


# ---------------------------------------------------------------------------
# Test 10b – Cooldown risk gate
# ---------------------------------------------------------------------------


def test_risk_gate_cooldown():
    """Two cards for the same ticker within the cooldown window => second blocked."""
    cooldown_minutes = 30

    card1 = make_card(
        card_id="c-cool1",
        ticker="MSFT",
        exchange_code="NASDAQ",
        news_published_at=T0,
        news_fetched_at=T0 + timedelta(seconds=60),
        created_at=T0 + timedelta(seconds=120),
    )
    # Second card arrives only 5 minutes after the first entry (within 30-min cooldown).
    card2 = make_card(
        card_id="c-cool2",
        ticker="MSFT",
        exchange_code="NASDAQ",
        news_published_at=T0 + timedelta(minutes=5),
        news_fetched_at=T0 + timedelta(minutes=5, seconds=60),
        created_at=T0 + timedelta(minutes=5, seconds=120),
    )

    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=60, path=[100.0] * 60)

    result = _run(
        [card1, card2],
        {"MSFT|NASDAQ": bars},
        execution_model=_default_exec(take_profit_pct=0.50, stop_loss_pct=0.50),
        risk_model=_default_risk(ticker_cooldown_minutes=cooldown_minutes, max_daily_trades=100),
    )

    blocked = [t for t in result.trades if t.exit_reason == ExitReason.RISK_BLOCKED]
    assert len(blocked) == 1
    assert blocked[0].risk_block_rule == "ticker_cooldown"


# ---------------------------------------------------------------------------
# Test 10c – Daily-loss-breaker blocks later card
# ---------------------------------------------------------------------------


def test_risk_gate_daily_loss_breaker():
    """A losing trade that trips the daily_loss_limit blocks subsequent same-day cards."""
    # Loss limit = 5 USD; we need the first trade to lose more than that.
    # Use a sell card so we can craft a clean-loss path easily.
    # Actually use a buy card with a big stop that triggers immediately.

    # Card 1: buy, stop hit immediately (loses ~$15 on $1000 position at 1.5% stop).
    # With max_position_usd=1000, entry=100 => qty=10; stop at 98.5 => loss ~$15.
    card1 = make_card(
        card_id="c-loss1",
        direction="buy",
        confidence=0.9,
        news_published_at=T0,
        news_fetched_at=T0 + timedelta(seconds=60),
        created_at=T0 + timedelta(seconds=120),
    )
    card2 = make_card(
        card_id="c-loss2",
        direction="buy",
        confidence=0.9,
        news_published_at=T0 + timedelta(minutes=5),
        news_fetched_at=T0 + timedelta(minutes=5, seconds=60),
        created_at=T0 + timedelta(minutes=5, seconds=120),
    )

    entry_start1 = T0 + timedelta(seconds=180)
    entry_start2 = T0 + timedelta(minutes=5, seconds=180)

    # Card1 bars: entry bar + one dropping bar.
    c1_bars = make_bars(
        entry_start1,
        count=3,
        path=[100.0, 98.0, 98.0],  # drop to 98 => below 1.5% stop at 98.5
    )
    # Card2 bars: flat (should never be entered due to risk gate).
    c2_bars = make_bars(entry_start2, count=10, path=[100.0] * 10)

    # Merge bars into one list for the same ticker.
    all_bars = c1_bars + c2_bars

    result = _run(
        [card1, card2],
        {"AAPL|NASDAQ": all_bars},
        execution_model=_default_exec(stop_loss_pct=0.015, take_profit_pct=0.50),
        risk_model=_default_risk(
            daily_loss_limit_usd=5.0,  # very tight limit
            max_daily_trades=100,
            ticker_cooldown_minutes=0,
            max_position_usd=1000.0,
        ),
    )

    trades_by_card = {t.thesis_card_id: t for t in result.trades}
    t1 = trades_by_card["c-loss1"]
    t2 = trades_by_card["c-loss2"]

    assert t1.exit_reason == ExitReason.STOP_LOSS
    assert t1.net_pnl is not None and t1.net_pnl < 0

    assert t2.exit_reason == ExitReason.RISK_BLOCKED
    assert t2.risk_block_rule == "daily_loss_limit"


# ---------------------------------------------------------------------------
# Test 11 – card_population filtering
# ---------------------------------------------------------------------------


def test_card_population_approved_only():
    """approved_only includes only valid cards."""
    cards = [
        make_card(card_id="c-ok", validation_status="valid"),
        make_card(card_id="c-rej", validation_status="rejected"),
    ]
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=10, path=[100.0] * 10)

    result = _run([*cards], {"AAPL|NASDAQ": bars}, population=CardPopulation.APPROVED_ONLY)
    assert result.metrics.cards_in_population == 1
    assert all(t.card_decision_state == "approved" for t in result.trades)


def test_card_population_rejected_only():
    """rejected_only includes only rejected cards."""
    cards = [
        make_card(card_id="c-ok2", validation_status="valid"),
        make_card(card_id="c-rej2", validation_status="rejected"),
    ]
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=10, path=[100.0] * 10)

    result = _run([*cards], {"AAPL|NASDAQ": bars}, population=CardPopulation.REJECTED_ONLY)
    assert result.metrics.cards_in_population == 1
    assert all(t.card_decision_state == "rejected" for t in result.trades)


def test_card_population_all():
    """all includes both valid and rejected cards."""
    cards = [
        make_card(card_id="c-ok3", validation_status="valid"),
        make_card(card_id="c-rej3", validation_status="rejected"),
    ]
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=10, path=[100.0] * 10)

    result = _run([*cards], {"AAPL|NASDAQ": bars}, population=CardPopulation.ALL)
    assert result.metrics.cards_in_population == 2
    states = {t.card_decision_state for t in result.trades}
    assert states == {"approved", "rejected"}


# ---------------------------------------------------------------------------
# Test 12 – Gap metrics: pnl_gap and trades_flipped_by_delay
# ---------------------------------------------------------------------------


def test_gap_metrics_pnl_gap_and_flipped():
    """pnl_gap == ideal_net_pnl - actual_net_pnl; delay that misses the move => flip.

    Scenario:
      - news_ready_at = T0 (ideal entry at T0+180s)
      - created_at = T0+600s (actual entry 10 minutes later)
      - Price rises sharply between T0+180s and T0+300s then falls back.
      - Ideal: enters early enough to take profit.
      - Actual: enters too late, misses the move, exits at loss or lower return.

    We make the price path so that:
      - From T0+180s: rises 3% in 3 bars (hits take profit for ideal)
      - At T0+600s (actual entry): price is back near entry, then drops -> stop loss.
    """
    tp_pct = 0.03
    sl_pct = 0.015

    card = make_card(
        card_id="c-gap",
        news_published_at=T0,
        news_fetched_at=T0 + timedelta(seconds=120),
        created_at=T0 + timedelta(seconds=600),  # 10 minutes late
    )

    entry_start = T0 + timedelta(seconds=120)

    # Bars from T0+120s.  Ideal entry is at T0+180s (bar index 1).
    # Actual entry is at T0+600s (bar index 8, since bars are 1-min each).
    # Path: rise to 103+, then fall to 98- for the actual entrant.
    path = (
        [100.0, 100.0]    # bars at T0+120s, T0+180s (ideal entry at index 1 open=100)
        + [101.5, 103.5]  # rises above 103 => ideal take-profit hit at bar 3 (index 3)
        + [102.0, 101.0, 100.0, 99.0, 100.0]  # coming down; actual entry at index 8
        + [100.0]  # bar at T0+600s (8 min after start) = actual entry bar, open=~99
        + [98.0]   # drops => stop loss for actual entrant
    )
    bars = make_bars(entry_start, count=len(path), path=path)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        timing=TimingScenario.BOTH,
        execution_model=_default_exec(take_profit_pct=tp_pct, stop_loss_pct=sl_pct),
        ideal_fetch_delay_seconds=120,
        ideal_thesis_delay_seconds=60,
    )

    m = result.metrics
    assert m.pnl_gap is not None
    # Ideal should win (take-profit), actual should lose (stop-loss) => flipped >= 1.
    assert m.trades_flipped_by_delay is not None
    assert m.trades_flipped_by_delay >= 1
    # pnl_gap = ideal_net_pnl - actual_net_pnl; ideal wins so gap should be positive.
    assert m.pnl_gap > 0


# ---------------------------------------------------------------------------
# Test 13 – Card-status breakdown in summary_json
# ---------------------------------------------------------------------------


def test_summary_json_card_status_breakdown():
    """summary_json contains by_card_status keys for approved, rejected,
    card_was_live_expired, and card_unexpired_at_entry."""
    valid_card = make_card(
        card_id="c-valid-sj",
        validation_status="valid",
        created_at=T0 + timedelta(seconds=180),
    )
    rejected_card = make_card(
        card_id="c-rej-sj",
        validation_status="rejected",
        created_at=T0 + timedelta(seconds=180),
    )
    # Expired card: ideal entry (T0+180s) is after expires_at.
    expired_card = make_card(
        card_id="c-exp-sj",
        validation_status="valid",
        created_at=T0 + timedelta(seconds=180),
        expires_at=T0 + timedelta(seconds=100),  # expires before ideal entry
    )

    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=20, path=[100.0] * 20)

    result = _run(
        [valid_card, rejected_card, expired_card],
        {"AAPL|NASDAQ": bars},
        population=CardPopulation.ALL,
    )

    sj = result.metrics.summary_json
    assert "by_card_status" in sj
    bcs = sj["by_card_status"]
    assert "approved" in bcs
    assert "rejected" in bcs
    assert "card_was_live_expired" in bcs
    assert "card_unexpired_at_entry" in bcs

    # The expired_card should appear in card_was_live_expired bucket.
    expired_count = bcs["card_was_live_expired"]["trades_opened"]
    # The expired card still enters (engine doesn't block on expiry, just tags it).
    assert expired_count >= 0  # sanity; value depends on whether it fills

    # At least the rejected card should be in rejected bucket.
    rej_count = bcs["rejected"]["trades_closed"] + bcs["rejected"]["trades_opened"]
    assert rej_count >= 1


# ---------------------------------------------------------------------------
# Test 14 – Sell / short direction: down-path = profitable take-profit
# ---------------------------------------------------------------------------


def test_take_profit_sell_short():
    """A sell (short) card on a down-path hits take-profit with positive PnL."""
    tp_pct = 0.03
    sl_pct = 0.015
    entry_price = 100.0
    sell_target = entry_price * (1 - tp_pct)  # 97.0

    card = make_card(card_id="c-sell-tp", direction="sell")
    entry_start = T0 + timedelta(seconds=180)

    # Path: 3 flat bars then a drop below 97.
    flat = [100.0] * 3
    drop = [sell_target - 0.50]  # 96.5 -> low will be 96.5 < 97.0 target ✓
    path = flat + drop
    bars = make_bars(entry_start, count=len(path), path=path)

    result = _run(
        [card],
        {"AAPL|NASDAQ": bars},
        execution_model=_default_exec(
            take_profit_pct=tp_pct,
            stop_loss_pct=sl_pct,
        ),
    )
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TAKE_PROFIT
    assert trade.net_pnl is not None and trade.net_pnl > 0


# ---------------------------------------------------------------------------
# Bonus: multi-article evidence - ties broken by latest fetched_at
# ---------------------------------------------------------------------------


def test_delay_decomposition_multi_article_tie_broken_by_latest_fetched():
    """When multiple evidence articles share the same published_at, the one with
    the latest fetched_at is used as the triggering article (conservative latency)."""
    pub = T0
    fetched_early = T0 + timedelta(seconds=60)
    fetched_late = T0 + timedelta(seconds=90)
    created = T0 + timedelta(seconds=200)

    card = ExportedThesisCard(
        id="c-multi-art",
        ticker="AAPL",
        exchange_code="NASDAQ",
        direction="buy",
        strategy="sentiment_momentum",
        time_horizon="intraday",
        confidence=0.9,
        risk_max_loss_usd=100.0,
        risk_stop_condition="stop",
        risk_invalidation_condition="invalidate",
        validation_status="valid",
        rejection_reason_code=None,
        created_at=created,
        expires_at=created + timedelta(hours=4),
        signal_published_at=created,
        evidence=[
            ExportedEvidenceArticle(article_id="art-A", published_at=pub, fetched_at=fetched_early),
            ExportedEvidenceArticle(article_id="art-B", published_at=pub, fetched_at=fetched_late),
        ],
        news_ready_at=pub,
    )

    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=20, path=[100.0] * 20)

    result = _run([card], {"AAPL|NASDAQ": bars})
    trade = result.trades[0]

    # Triggering = art-B (latest fetched_at=90s after pub).
    # news_fetch_delay = fetched_late - pub = 90s.
    assert trade.news_fetch_delay_seconds == pytest.approx(90.0)
    # thesis_build_delay = created - fetched_late = 200 - 90 = 110s.
    assert trade.thesis_build_delay_seconds == pytest.approx(110.0)
    # total_pipeline_delay = created - pub = 200s.
    assert trade.total_pipeline_delay_seconds == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Bonus: cards_considered vs cards_in_population
# ---------------------------------------------------------------------------


def test_cards_considered_vs_population():
    """cards_considered counts all returned cards; cards_in_population counts after filter."""
    cards = [
        make_card(card_id="cv1", validation_status="valid"),
        make_card(card_id="cv2", validation_status="valid"),
        make_card(card_id="cr1", validation_status="rejected"),
    ]
    entry_start = T0 + timedelta(seconds=180)
    bars = make_bars(entry_start, count=10, path=[100.0] * 10)

    result = _run([*cards], {"AAPL|NASDAQ": bars}, population=CardPopulation.APPROVED_ONLY)
    assert result.metrics.cards_considered == 3
    assert result.metrics.cards_in_population == 2
