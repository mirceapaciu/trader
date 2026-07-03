from datetime import timedelta

from src.product_components.trade_executor.broker.fake_gateway import InMemoryBrokerGateway
from src.product_components.trade_executor.broker.gateway import (
    AccountSnapshot,
    BrokerEvent,
    BrokerEventKind,
    QuoteSnapshot,
)
from src.product_components.trade_executor.models import SignalMessage
from src.product_components.trade_executor.service import TradeExecutorRunner
from src.product_components.trade_executor.settings import TradeExecutorSettings

from tests.product_components.trade_executor.fakes import (
    FakeCalendar,
    FakeMarketContextClient,
    FakeContext,
    FakeRedisIo,
    FakeRepository,
    FakeReviewReader,
    FakeSectorReader,
    FakeWatchlistReader,
    utc,
)

NOW = utc()


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _payload(card_id="c1", **overrides) -> dict:
    payload = {
        "thesis_card_id": card_id,
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "direction": "buy",
        "time_horizon": "swing_1d_5d",
        "strategy": "event_driven",
        "confidence": 0.8,
        "risk_box": {"max_loss_usd": 120.0, "stop_condition": "x", "invalidation_condition": None},
        "source_analysis_ids": [1],
        "created_at": "2026-07-03T14:00:00Z",
        "expires_at": "2026-07-03T18:00:00Z",
    }
    payload.update(overrides)
    return payload


def _message(card_id="c1", event_type="thesis_card.created", **overrides) -> SignalMessage:
    return SignalMessage(
        message_id=f"m-{card_id}",
        event_id=f"evt-{card_id}",
        event_type=event_type,
        dedupe_key=card_id,
        payload=_payload(card_id, **overrides),
    )


def _quote(as_of=NOW, bid=99.9, ask=100.0) -> QuoteSnapshot:
    return QuoteSnapshot("AAPL", "XNAS", bid=bid, ask=ask, last=100.0, as_of=as_of)


def _build(
    *,
    messages=None,
    review="approved",
    watchlist=True,
    atr=2.0,
    sector=None,
    quote=None,
    monotonic=None,
    broker=None,
):
    broker = broker or InMemoryBrokerGateway()
    if quote is not None:
        broker.set_quote(quote)
    repo = FakeRepository()
    redis_io = FakeRedisIo(messages=messages or [])
    runner = TradeExecutorRunner(
        settings=TradeExecutorSettings.from_env(),
        broker=broker,
        repository=repo,
        redis_io=redis_io,
        market_context_client=FakeMarketContextClient(FakeContext(atr_20d=atr)),
        review_reader=FakeReviewReader(review),
        watchlist_reader=FakeWatchlistReader(active=watchlist, present=watchlist),
        sector_reader=FakeSectorReader(sector),
        calendar=FakeCalendar(),
        clock=lambda: NOW,
        monotonic=monotonic or _Clock(),
    )
    return runner, repo, redis_io, broker


# --- admission / happy path ---------------------------------------------------


def test_admitted_card_submits_bracket() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    runner.process_message(_message())

    assert len(broker.submitted_brackets) == 1
    request, _handle = broker.submitted_brackets[0]
    assert request.side == "buy"
    assert request.quantity >= 1
    # one passed decision + 3 execution legs
    assert len(repo._decisions) == 1
    assert list(repo._decisions.values())[0]["risk_check_passed"] is True
    assert len(repo._executions) == 3
    assert redis_io.acked == ["m-c1"]
    # trades_count incremented for the day
    assert list(repo._daily.values())[0]["trades_count"] == 1


def test_short_card_admitted() -> None:
    runner, repo, redis_io, broker = _build(
        quote=QuoteSnapshot("AAPL", "XNAS", bid=99.9, ask=100.0, last=100.0, as_of=NOW),
        messages=None,
    )
    runner.process_message(_message(direction="sell"))
    assert broker.submitted_brackets[0][0].side == "sell"


# --- admission drops ----------------------------------------------------------


def _reason(repo) -> str:
    return list(repo._decisions.values())[0]["risk_check_details"]


def test_direction_hold_dropped() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    runner.process_message(_message(direction="hold"))
    assert _reason(repo) == "direction_hold"
    assert broker.submitted_brackets == []
    assert redis_io.acked == ["m-c1"]


def test_not_in_watchlist_dropped() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote(), watchlist=False)
    runner.process_message(_message())
    assert _reason(repo) == "not_in_watchlist"
    assert broker.submitted_brackets == []


def test_review_not_approved_dropped() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote(), review=None)
    runner.process_message(_message())
    assert _reason(repo) == "review_not_approved"


# --- fail-closed --------------------------------------------------------------


def test_quote_unavailable_fails_closed() -> None:
    runner, repo, redis_io, broker = _build()  # no quote set
    runner.process_message(_message())
    assert _reason(repo) == "quote_unavailable"
    assert broker.submitted_brackets == []
    assert redis_io.acked == ["m-c1"]


def test_stale_quote_fails_closed() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote(as_of=NOW - timedelta(minutes=5)))
    runner.process_message(_message())
    assert _reason(repo) == "quote_unavailable"


def test_atr_unavailable_fails_closed() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote(), atr=None)
    runner.process_message(_message())
    assert _reason(repo) == "atr_unavailable"


def test_size_below_one_share_rejected() -> None:
    # max_loss tiny + wide ATR -> qty floor 0
    runner, repo, redis_io, broker = _build(quote=_quote(), atr=50.0)
    runner.process_message(_message(risk_box={"max_loss_usd": 1.0, "stop_condition": "x"}))
    assert _reason(repo) == "size_below_one_share"
    assert broker.submitted_brackets == []


# --- duplicate / idempotency --------------------------------------------------


def test_duplicate_card_not_resubmitted() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    runner.process_message(_message())
    runner.process_message(_message())  # same card_id c1
    assert len(broker.submitted_brackets) == 1
    assert redis_io.acked == ["m-c1", "m-c1"]


# --- risk gate ----------------------------------------------------------------


def test_daily_loss_halt_blocks_and_latches() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    broker.set_account_snapshot(AccountSnapshot(total_unrealized_pnl=-250.0))
    runner.process_message(_message())
    assert _reason(repo) == "daily_loss_halt"
    assert broker.submitted_brackets == []
    # halt latched for the day
    assert list(repo._daily.values())[0]["halted"] is True


def test_max_positions_blocks() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    # pre-fill 5 open positions
    for i in range(5):
        repo.open_position(
            thesis_card_id=f"x{i}", decision_id=100 + i, ticker=f"T{i}", exchange_code="XNAS",
            side="long", quantity=1, avg_entry_price=10.0, stop_price=9.0, take_profit_price=11.0,
            time_exit_at=None, opened_at=NOW,
        )
    runner.process_message(_message())
    assert _reason(repo) == "portfolio_cap_exceeded"


# --- lifecycle ----------------------------------------------------------------


def _submit_and_get_ids(runner, broker):
    runner.process_message(_message())
    request, handle = broker.submitted_brackets[0]
    return handle


def test_entry_fill_opens_position_with_time_exit() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    handle = _submit_and_get_ids(runner, broker)

    broker.enqueue_event(BrokerEvent(
        kind=BrokerEventKind.ORDER_STATUS, ts=NOW, ibkr_order_id=handle.entry_order_id,
        status="Filled", filled_qty=10, avg_fill_price=100.1,
    ))
    runner._apply_broker_events()

    positions = repo.list_open_positions()
    assert len(positions) == 1
    assert positions[0].side == "long"
    assert positions[0].avg_entry_price == 100.1
    # time_exit_at = fill + 5 days (FakeCalendar)
    assert positions[0].time_exit_at == NOW + timedelta(days=5)


def test_stop_fill_closes_position_and_cancels_sibling() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    handle = _submit_and_get_ids(runner, broker)
    broker.enqueue_event(BrokerEvent(
        kind=BrokerEventKind.ORDER_STATUS, ts=NOW, ibkr_order_id=handle.entry_order_id,
        status="Filled", filled_qty=10, avg_fill_price=100.0,
    ))
    runner._apply_broker_events()

    broker.enqueue_event(BrokerEvent(
        kind=BrokerEventKind.ORDER_STATUS, ts=NOW, ibkr_order_id=handle.stop_order_id,
        status="Filled", filled_qty=10, avg_fill_price=97.0,
    ))
    runner._apply_broker_events()

    assert repo.list_open_positions() == []
    closed = repo.get_position_by_id(1)
    assert closed.exit_reason == "stop"
    # realized = (97 - 100) * 10 = -30
    assert closed.realized_pnl == -30.0
    assert handle.take_profit_order_id in broker.cancelled_order_ids
    # realized pnl booked to the day
    assert list(repo._daily.values())[0]["realized_pnl"] == -30.0


def test_time_exit_flattens_position() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    handle = _submit_and_get_ids(runner, broker)
    broker.enqueue_event(BrokerEvent(
        kind=BrokerEventKind.ORDER_STATUS, ts=NOW, ibkr_order_id=handle.entry_order_id,
        status="Filled", filled_qty=10, avg_fill_price=100.0,
    ))
    runner._apply_broker_events()

    # advance past the time_exit window
    later = NOW + timedelta(days=6)
    runner._clock = lambda: later
    runner._evaluate_time_exits(later)

    assert len(broker.submitted_flattens) == 1
    flatten_req, flatten_order_id = broker.submitted_flattens[0]
    assert flatten_req.side_to_close == "sell"

    # flatten fill closes the position with reason 'time'
    broker.enqueue_event(BrokerEvent(
        kind=BrokerEventKind.ORDER_STATUS, ts=later, ibkr_order_id=flatten_order_id,
        status="Filled", filled_qty=10, avg_fill_price=101.0,
    ))
    runner._apply_broker_events()
    closed = repo.get_position_by_id(1)
    assert closed.exit_reason == "time"


def test_fill_timeout_reprices_then_abandons() -> None:
    clock = _Clock(0.0)
    runner, repo, redis_io, broker = _build(quote=_quote(), monotonic=clock)
    handle = _submit_and_get_ids(runner, broker)

    # advance past the fill timeout (default 30s) -> re-price once
    clock.value = 100.0
    runner._check_fill_timeouts()
    assert broker.repriced and broker.repriced[0][0] == handle.entry_order_id

    # still unfilled after another timeout -> abandon (cancel all legs)
    clock.value = 200.0
    runner._check_fill_timeouts()
    assert handle.entry_order_id in broker.cancelled_order_ids
    assert handle.stop_order_id in broker.cancelled_order_ids


# --- retry / DLQ / ignore -----------------------------------------------------


def test_non_thesis_event_acked_and_ignored() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    runner.process_message(_message(event_type="something.else"))
    assert redis_io.acked == ["m-c1"]
    assert repo._decisions == {}


def test_malformed_card_dead_lettered() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())
    msg = SignalMessage(
        message_id="m-bad", event_id="e", event_type="thesis_card.created",
        dedupe_key="bad", payload={"thesis_card_id": "bad"},  # missing required fields
    )
    runner.process_message(msg)
    assert redis_io.dlq == [("m-bad", "malformed_thesis_card")]
    assert redis_io.acked == ["m-bad"]


def test_transient_error_leaves_message_pending_then_dlq() -> None:
    runner, repo, redis_io, broker = _build(quote=_quote())

    def _boom(**kwargs):
        raise RuntimeError("db down")

    repo.has_open_or_working_for_instrument = _boom  # type: ignore

    msg = _message()
    redis_io.delivery_counts["m-c1"] = 1
    runner._process_with_retry(msg)
    assert redis_io.acked == []  # left pending

    redis_io.delivery_counts["m-c1"] = 5  # exceed max_delivery_attempts (default 5)
    runner._process_with_retry(msg)
    assert redis_io.dlq and redis_io.dlq[0][0] == "m-c1"
    assert redis_io.acked == ["m-c1"]
