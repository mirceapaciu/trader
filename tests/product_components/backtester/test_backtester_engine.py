from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core_components.backtest_engine import Bar
from src.product_components.backtester.engine import BacktesterEngine
from src.product_components.backtester.models import (
    BacktestRunParams,
    CardPopulation,
    ExecutionMode,
    ExecutionModel,
    ExitReason,
    TimingScenario,
)
from src.product_components.thesis_builder.export import (
    ExportedEvidenceArticle,
    ExportedThesisCard,
)

UTC = timezone.utc
T0 = datetime(2025, 1, 2, 14, 0, tzinfo=UTC)


def _card(
    *,
    card_id: str = "card-1",
    direction: str = "buy",
    validation_status: str = "valid",
    confidence: float = 0.9,
    created_offset_s: int = 180,
    strategy: str = "sentiment_momentum",
) -> ExportedThesisCard:
    published = T0
    fetched = T0 + timedelta(seconds=120)
    created = T0 + timedelta(seconds=created_offset_s)
    return ExportedThesisCard(
        id=card_id,
        ticker="AAPL",
        exchange_code="NASDAQ",
        direction=direction,
        strategy=strategy,
        time_horizon="intraday",
        confidence=confidence,
        risk_max_loss_usd=100.0,
        risk_stop_condition="stop",
        risk_invalidation_condition="invalidate",
        validation_status=validation_status,
        rejection_reason_code=None,
        created_at=created,
        expires_at=created + timedelta(hours=4),
        signal_published_at=created,
        evidence=[
            ExportedEvidenceArticle(
                article_id="art-1", published_at=published, fetched_at=fetched
            )
        ],
        news_ready_at=published,
    )


class FakeCards:
    def __init__(self, cards):
        self._cards = cards

    def export_cards(self, *, window_start_at, window_end_at, validation_status=None, strategy=None):
        return list(self._cards)


class FakeBars:
    def __init__(self, bars):
        self._bars = bars

    def historical_bars(self, *, ticker, exchange_code, interval, start, end):
        return list(self._bars)


def _bars_rising(start: datetime, count: int = 30, step_pct: float = 0.002):
    bars = []
    price = 100.0
    for i in range(count):
        ts = start + timedelta(minutes=i)
        nxt = price * (1 + step_pct)
        bars.append(Bar(start_at=ts, open=price, high=nxt, low=price, close=nxt, volume=1000))
        price = nxt
    return bars


def _params(timing=TimingScenario.IDEAL, population=CardPopulation.ALL):
    return BacktestRunParams(
        run_id="run-1",
        window_start_at=T0 - timedelta(minutes=5),
        window_end_at=T0 + timedelta(hours=6),
        timing_scenario=timing,
        card_population=population,
        execution_model=ExecutionModel(mode=ExecutionMode.LEGACY_FLAT_PERCENT),
    )


def test_take_profit_buy_produces_closed_winning_trade():
    # Ideal entry = news_ready_at + 120 + 60 = T0 + 180s.
    entry = T0 + timedelta(seconds=180)
    engine = BacktesterEngine(
        params=_params(),
        cards_provider=FakeCards([_card()]),
        bars_provider=FakeBars(_bars_rising(entry)),
    )
    result = engine.run()
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == ExitReason.TAKE_PROFIT
    assert trade.is_closed
    assert trade.net_pnl is not None
    assert result.metrics.trades_closed == 1
    assert result.metrics.win_rate == 1.0


def test_no_price_history_marks_not_filled_and_skipped():
    engine = BacktesterEngine(
        params=_params(),
        cards_provider=FakeCards([_card()]),
        bars_provider=FakeBars([]),
    )
    result = engine.run()
    assert result.trades[0].exit_reason == ExitReason.NOT_FILLED
    assert result.metrics.cards_skipped_no_price == 1
    assert result.metrics.trades_closed == 0


def test_approved_only_population_excludes_rejected():
    cards = [
        _card(card_id="c-approved", validation_status="valid"),
        _card(card_id="c-rejected", validation_status="rejected"),
    ]
    entry = T0 + timedelta(seconds=180)
    engine = BacktesterEngine(
        params=_params(population=CardPopulation.APPROVED_ONLY),
        cards_provider=FakeCards(cards),
        bars_provider=FakeBars(_bars_rising(entry)),
    )
    result = engine.run()
    assert result.metrics.cards_in_population == 1
    assert all(t.card_decision_state == "approved" for t in result.trades)


def test_both_scenario_emits_two_rows_per_card_and_gap_metrics():
    entry = T0 + timedelta(seconds=120)
    engine = BacktesterEngine(
        params=_params(timing=TimingScenario.BOTH),
        cards_provider=FakeCards([_card()]),
        bars_provider=FakeBars(_bars_rising(entry, count=60)),
    )
    result = engine.run()
    scenarios = {t.entry_timing_scenario for t in result.trades}
    assert scenarios == {"ideal", "actual"}
    assert result.metrics.pnl_gap is not None


def test_delays_recorded_on_trade():
    entry = T0 + timedelta(seconds=180)
    engine = BacktesterEngine(
        params=_params(),
        cards_provider=FakeCards([_card()]),
        bars_provider=FakeBars(_bars_rising(entry)),
    )
    trade = engine.run().trades[0]
    assert trade.news_fetch_delay_seconds == 120.0
    assert trade.thesis_build_delay_seconds == 60.0
    assert trade.total_pipeline_delay_seconds == 180.0


def test_negative_thesis_build_delay_clamped_to_none():
    # When an article is re-fetched after card creation (fetched_at > created_at),
    # thesis_build_delay would be negative — clamp to None to satisfy DB constraint.
    published = T0
    fetched_late = T0 + timedelta(hours=2)  # re-fetched well after card creation
    created = T0 + timedelta(seconds=300)   # card created 5 min after publish
    card = ExportedThesisCard(
        id="card-late-fetch",
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
            ExportedEvidenceArticle(
                article_id="art-late", published_at=published, fetched_at=fetched_late
            )
        ],
        news_ready_at=published,
    )
    entry = created + timedelta(seconds=180)
    engine = BacktesterEngine(
        params=_params(),
        cards_provider=FakeCards([card]),
        bars_provider=FakeBars(_bars_rising(entry)),
    )
    trade = engine.run().trades[0]
    assert trade.thesis_build_delay_seconds is None
    assert trade.news_fetch_delay_seconds is not None  # fetch delay is still valid
    assert trade.total_pipeline_delay_seconds == 300.0  # created - published = 300s
