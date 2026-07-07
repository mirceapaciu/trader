from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core_components.backtest_engine import Bar
from src.product_components.backtester.engine import BacktesterEngine
from src.product_components.backtester.models import (
    BacktestRunParams,
    ExecutionMode,
    ExecutionModel,
    ExitReason,
    RiskModel,
)
from src.product_components.thesis_builder.export import (
    ExportedEvidenceArticle,
    ExportedThesisCard,
)
from src.product_components.trade_executor.pipeline import (
    construct_levels,
    entry_limit_price,
    size_position,
)

UTC = timezone.utc
T0 = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)
ENTRY = T0 + timedelta(seconds=180)


class FakeCalendar:
    def time_exit_at(self, *, fill_time: datetime, trading_days: int) -> datetime:
        return fill_time + timedelta(days=trading_days)

    def is_rth(self, now: datetime) -> bool:
        return True

    def next_session_open(self, now: datetime) -> datetime:
        return now


class FakeCards:
    def __init__(self, cards: list[ExportedThesisCard]) -> None:
        self._cards = cards

    def export_cards(self, *, window_start_at, window_end_at, validation_status=None, strategy=None):
        return list(self._cards)


class FakeBars:
    def __init__(
        self,
        *,
        intraday_by_key: dict[str, list[Bar]],
        daily_by_key: dict[str, list[Bar]] | None = None,
    ) -> None:
        self.intraday_by_key = intraday_by_key
        self.daily_by_key = daily_by_key or {}
        self.calls: list[dict] = []

    def historical_bars(self, *, ticker, exchange_code, interval, start, end):
        self.calls.append(
            {
                "ticker": ticker,
                "exchange_code": exchange_code,
                "interval": interval,
                "start": start,
                "end": end,
            }
        )
        key = f"{ticker}|{exchange_code}"
        bars = self.daily_by_key.get(key, []) if interval == "1d" else self.intraday_by_key.get(key, [])
        start_utc = _utc(start)
        end_utc = _utc(end)
        return [bar for bar in bars if start_utc <= _utc(bar.start_at) <= end_utc]


def _card(
    *,
    card_id: str = "card-1",
    ticker: str = "AAPL",
    exchange_code: str = "XNAS",
    validation_status: str = "valid",
    confidence: float = 0.9,
    time_horizon: str = "swing_1d_5d",
    risk_max_loss_usd: float = 120.0,
    created_at: datetime = ENTRY,
    expires_at: datetime | None = None,
) -> ExportedThesisCard:
    return ExportedThesisCard(
        id=card_id,
        ticker=ticker,
        exchange_code=exchange_code,
        direction="buy",
        strategy="sentiment_momentum",
        time_horizon=time_horizon,
        confidence=confidence,
        risk_max_loss_usd=risk_max_loss_usd,
        risk_stop_condition="stop",
        risk_invalidation_condition="invalidate",
        validation_status=validation_status,
        rejection_reason_code=None,
        created_at=created_at,
        expires_at=expires_at or created_at + timedelta(days=10),
        signal_published_at=created_at,
        evidence=[
            ExportedEvidenceArticle(
                article_id=f"art-{card_id}",
                published_at=T0,
                fetched_at=T0,
            )
        ],
        news_ready_at=T0,
    )


def _intraday_bars(entry: datetime = ENTRY, *, path: list[float] | None = None) -> list[Bar]:
    prices = path or [100.0, 100.0, 100.0, 100.0]
    bars: list[Bar] = []
    prev = prices[0]
    for index, close in enumerate(prices):
        start = entry + timedelta(minutes=index)
        open_ = prev if index else close
        bars.append(
            Bar(
                start_at=start,
                open=open_,
                high=max(open_, close),
                low=min(open_, close),
                close=close,
                volume=1000,
            )
        )
        prev = close
    return bars


def _flat_until_time_exit(entry: datetime = ENTRY) -> list[Bar]:
    return [
        Bar(start_at=entry, open=100.0, high=100.0, low=100.0, close=100.0, volume=1000),
        Bar(
            start_at=entry + timedelta(hours=4),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
        ),
        Bar(
            start_at=entry + timedelta(days=5),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
        ),
    ]


def _daily_bars_before_entry(entry: datetime = ENTRY, *, true_range: float = 2.0) -> list[Bar]:
    bars: list[Bar] = []
    start = entry - timedelta(days=30)
    for index in range(21):
        day = start + timedelta(days=index)
        bars.append(
            Bar(
                start_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
                open=100.0,
                high=100.0 + true_range / 2,
                low=100.0 - true_range / 2,
                close=100.0,
                volume=1000,
            )
        )
    return bars


def _params(
    *,
    risk_model: RiskModel | None = None,
    execution_model: ExecutionModel | None = None,
) -> BacktestRunParams:
    return BacktestRunParams(
        run_id="run-live",
        window_start_at=T0 - timedelta(minutes=5),
        window_end_at=ENTRY + timedelta(days=6),
        execution_model=execution_model
        or ExecutionModel(
            mode=ExecutionMode.LIVE_PARITY,
            entry_limit_slippage_bps=5.0,
            atr_stop_mult=1.5,
            take_profit_r=2.0,
            time_horizon_days_map={"swing_1d_5d": 5},
        ),
        risk_model=risk_model
        or RiskModel(
            max_position_usd=10_000.0,
            max_portfolio_exposure_usd=50_000.0,
            daily_loss_limit_usd=1_000_000.0,
            max_daily_trades=100,
        ),
    )


def _run(cards: list[ExportedThesisCard], bars: FakeBars, params: BacktestRunParams | None = None):
    return BacktesterEngine(
        params=params or _params(),
        cards_provider=FakeCards(cards),
        bars_provider=bars,
        trading_calendar=FakeCalendar(),
    ).run()


def test_live_parity_levels_quantity_and_time_exit_match_pipeline_calls() -> None:
    card = _card()
    bars = FakeBars(
        intraday_by_key={"AAPL|XNAS": _flat_until_time_exit()},
        daily_by_key={"AAPL|XNAS": _daily_bars_before_entry()},
    )

    trade = _run([card], bars).trades[0]

    entry = entry_limit_price(direction="buy", bid=100.0, ask=100.0, slippage_bps=5.0)
    levels = construct_levels(
        direction="buy",
        entry=entry,
        atr_20d=2.0,
        atr_stop_mult=1.5,
        take_profit_r=2.0,
    )
    order = size_position(
        max_loss_usd=120.0,
        entry=levels.entry,
        stop=levels.stop,
        max_position_size=10_000.0,
        portfolio_headroom=50_000.0,
    )

    assert trade.entry_price == pytest.approx(levels.entry)
    assert trade.quantity == order.quantity
    assert trade.exit_reason == ExitReason.TIME_STOP
    assert trade.exit_at == ENTRY + timedelta(days=5)
    assert trade.holding_period_seconds == pytest.approx(5 * 24 * 60 * 60)


def test_live_parity_atr_ignores_same_day_and_future_daily_bars() -> None:
    card = _card()
    daily = _daily_bars_before_entry(true_range=2.0)
    daily.extend(
        [
            Bar(
                start_at=datetime(ENTRY.year, ENTRY.month, ENTRY.day, tzinfo=UTC),
                open=100.0,
                high=200.0,
                low=1.0,
                close=100.0,
                volume=1000,
            ),
            Bar(
                start_at=ENTRY + timedelta(days=1),
                open=100.0,
                high=250.0,
                low=1.0,
                close=100.0,
                volume=1000,
            ),
        ]
    )
    bars = FakeBars(
        intraday_by_key={"AAPL|XNAS": _intraday_bars(path=[100.0, 106.2])},
        daily_by_key={"AAPL|XNAS": daily},
    )

    trade = _run([card], bars).trades[0]

    expected_entry = entry_limit_price(direction="buy", bid=100.0, ask=100.0, slippage_bps=5.0)
    expected_levels = construct_levels(
        direction="buy",
        entry=expected_entry,
        atr_20d=2.0,
        atr_stop_mult=1.5,
        take_profit_r=2.0,
    )
    assert trade.entry_price == pytest.approx(expected_levels.entry)
    assert trade.exit_reason == ExitReason.TAKE_PROFIT
    assert trade.exit_price == pytest.approx(expected_levels.target * (1 - 5.0 / 10_000))


@pytest.mark.parametrize(
    ("card", "expected_rule"),
    [
        (_card(card_id="rejected", validation_status="rejected"), "review_not_approved"),
        (_card(card_id="expired", expires_at=ENTRY - timedelta(seconds=1)), "card_expired"),
        (_card(card_id="low-confidence", confidence=0.5), "below_min_confidence"),
        (_card(card_id="unmapped", time_horizon="intraday"), "horizon_unmapped"),
    ],
)
def test_live_parity_admission_gate_blocks_with_pipeline_reason(
    card: ExportedThesisCard, expected_rule: str
) -> None:
    bars = FakeBars(intraday_by_key={"AAPL|XNAS": _intraday_bars()})

    trade = _run([card], bars).trades[0]

    assert trade.exit_reason == ExitReason.RISK_BLOCKED
    assert trade.risk_block_rule == expected_rule


def test_live_parity_risk_gate_blocks_max_daily_trades() -> None:
    first = _card(card_id="first", ticker="AAPL", created_at=ENTRY)
    second = _card(card_id="second", ticker="MSFT", created_at=ENTRY + timedelta(minutes=1))
    third = _card(card_id="third", ticker="GOOG", created_at=ENTRY + timedelta(minutes=2))
    intraday = _flat_until_time_exit()
    bars = FakeBars(
        intraday_by_key={
            "AAPL|XNAS": intraday,
            "MSFT|XNAS": [bar for bar in intraday],
            "GOOG|XNAS": [bar for bar in intraday],
        },
        daily_by_key={
            "AAPL|XNAS": _daily_bars_before_entry(),
            "MSFT|XNAS": _daily_bars_before_entry(),
            "GOOG|XNAS": _daily_bars_before_entry(),
        },
    )

    result = _run(
        [first, second, third],
        bars,
        _params(
            risk_model=RiskModel(
                max_position_usd=10_000.0,
                max_positions=1,
                max_portfolio_exposure_usd=50_000.0,
                daily_loss_limit_usd=1_000_000.0,
                max_daily_trades=1,
            )
        ),
    )

    blocked = {trade.thesis_card_id: trade.risk_block_rule for trade in result.trades}
    assert blocked["second"] == "max_daily_trades_reached"
    assert blocked["third"] == "max_daily_trades_reached"


def test_live_parity_risk_gate_blocks_max_positions() -> None:
    first = _card(card_id="first", ticker="AAPL", created_at=ENTRY)
    second = _card(card_id="second", ticker="MSFT", created_at=ENTRY + timedelta(minutes=1))
    intraday = _flat_until_time_exit()
    bars = FakeBars(
        intraday_by_key={
            "AAPL|XNAS": intraday,
            "MSFT|XNAS": [bar for bar in intraday],
        },
        daily_by_key={
            "AAPL|XNAS": _daily_bars_before_entry(),
            "MSFT|XNAS": _daily_bars_before_entry(),
        },
    )

    result = _run(
        [first, second],
        bars,
        _params(
            risk_model=RiskModel(
                max_position_usd=10_000.0,
                max_positions=1,
                max_portfolio_exposure_usd=50_000.0,
                daily_loss_limit_usd=1_000_000.0,
                max_daily_trades=100,
            )
        ),
    )

    by_card = {trade.thesis_card_id: trade for trade in result.trades}
    assert by_card["second"].exit_reason == ExitReason.RISK_BLOCKED
    assert by_card["second"].risk_block_rule == "portfolio_cap_exceeded"


def test_live_parity_daily_loss_halt_latches_for_remainder_of_day() -> None:
    first = _card(card_id="a-loss", ticker="AAPL", created_at=ENTRY)
    second = _card(card_id="b-halt-trigger", ticker="MSFT", created_at=ENTRY + timedelta(minutes=10))
    third = _card(card_id="c-halt-latched", ticker="GOOG", created_at=ENTRY + timedelta(minutes=20))
    loss_path = [100.0, 96.0, 96.0]
    flat = _flat_until_time_exit()
    bars = FakeBars(
        intraday_by_key={
            "AAPL|XNAS": _intraday_bars(path=loss_path),
            "MSFT|XNAS": flat,
            "GOOG|XNAS": flat,
        },
        daily_by_key={
            "AAPL|XNAS": _daily_bars_before_entry(),
            "MSFT|XNAS": _daily_bars_before_entry(),
            "GOOG|XNAS": _daily_bars_before_entry(),
        },
    )

    result = _run(
        [first, second, third],
        bars,
        _params(
            risk_model=RiskModel(
                max_position_usd=10_000.0,
                max_positions=10,
                max_portfolio_exposure_usd=50_000.0,
                daily_loss_limit_usd=10.0,
                max_daily_trades=100,
            )
        ),
    )

    by_card = {trade.thesis_card_id: trade for trade in result.trades}
    assert by_card["a-loss"].exit_reason == ExitReason.STOP_LOSS
    assert by_card["a-loss"].net_pnl is not None and by_card["a-loss"].net_pnl < -10.0
    assert by_card["b-halt-trigger"].risk_block_rule == "daily_loss_halt"
    assert by_card["c-halt-latched"].risk_block_rule == "daily_loss_halt"


def test_execution_model_snapshot_distinguishes_modes() -> None:
    live = ExecutionModel(mode=ExecutionMode.LIVE_PARITY).snapshot()
    legacy = ExecutionModel(mode=ExecutionMode.LEGACY_FLAT_PERCENT).snapshot()

    assert live["mode"] == "live_parity"
    assert legacy["mode"] == "legacy_flat_percent"
    assert "time_horizon_days_map" in live
    assert "take_profit_pct" in legacy


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
