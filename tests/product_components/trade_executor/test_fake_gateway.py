from datetime import datetime, timezone

from src.product_components.trade_executor.broker.fake_gateway import InMemoryBrokerGateway
from src.product_components.trade_executor.broker.gateway import (
    BracketRequest,
    BrokerEvent,
    BrokerEventKind,
    FlattenRequest,
    QuoteSnapshot,
)

NOW = datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc)


def _bracket() -> BracketRequest:
    return BracketRequest(
        ticker="AAPL", exchange_code="XNAS", side="buy", quantity=10,
        entry_limit_price=100.1, stop_price=97.0, take_profit_price=106.0,
        oca_group="oca-1", outside_rth=False,
    )


def test_submit_bracket_allocates_three_distinct_order_ids() -> None:
    gw = InMemoryBrokerGateway()
    handle = gw.submit_bracket(_bracket())
    ids = {handle.entry_order_id, handle.stop_order_id, handle.take_profit_order_id}
    assert len(ids) == 3
    assert handle.oca_group == "oca-1"
    assert gw.submitted_brackets[0][0].quantity == 10


def test_enqueue_and_drain_events_clears_buffer() -> None:
    gw = InMemoryBrokerGateway()
    gw.enqueue_event(BrokerEvent(kind=BrokerEventKind.ORDER_STATUS, ts=NOW, ibkr_order_id=1))
    drained = gw.drain_events()
    assert len(drained) == 1
    assert gw.drain_events() == []


def test_quote_roundtrip() -> None:
    gw = InMemoryBrokerGateway()
    quote = QuoteSnapshot("AAPL", "XNAS", bid=99.9, ask=100.0, last=100.0, as_of=NOW)
    gw.set_quote(quote)
    assert gw.snapshot_quote(ticker="AAPL", exchange_code="XNAS", timeout_seconds=5) is quote
    assert gw.snapshot_quote(ticker="MSFT", exchange_code="XNAS", timeout_seconds=5) is None


def test_flatten_and_cancel_recorded() -> None:
    gw = InMemoryBrokerGateway()
    gw.submit_flatten(FlattenRequest("AAPL", "XNAS", "sell", 10, 99.0, False))
    gw.cancel_order(42)
    gw.replace_order_price(7, 101.0)
    assert gw.submitted_flattens[0][0].quantity == 10
    assert gw.cancelled_order_ids == [42]
    assert gw.repriced == [(7, 101.0)]


def test_connection_toggle() -> None:
    gw = InMemoryBrokerGateway()
    assert gw.is_connected() is True
    gw.set_connected(False)
    assert gw.is_connected() is False
