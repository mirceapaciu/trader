from src.product_components.trade_executor.broker.fake_gateway import InMemoryBrokerGateway
from src.product_components.trade_executor.broker.gateway import OpenOrder
from src.product_components.trade_executor.repository import DecisionRecord
from src.product_components.trade_executor.service import TradeExecutorRunner
from src.product_components.trade_executor.settings import TradeExecutorSettings

from tests.product_components.trade_executor.fakes import FakeRedisIo, FakeRepository, utc

NOW = utc()


def _passed_decision(card_id: str, ticker: str) -> DecisionRecord:
    return DecisionRecord(
        id=None, thesis_card_id=card_id, ticker=ticker, exchange_code="XNAS", action="buy",
        quantity=10, order_type="limit", limit_price=100.0, entry_price=100.0, stop_price=97.0,
        take_profit_price=106.0, atr_20d=2.0, risk_amount_usd=120.0, confidence=0.8,
        signal_strength=None, source_analysis_ids=[1], risk_check_passed=True,
        risk_check_details="admitted", decided_at=NOW,
    )


def _runner(repo, broker):
    return TradeExecutorRunner(
        settings=TradeExecutorSettings.from_env(),
        broker=broker,
        repository=repo,
        redis_io=FakeRedisIo(),
    )


def test_passed_decision_without_executions_is_orphaned() -> None:
    repo = FakeRepository()
    broker = InMemoryBrokerGateway()
    decision_id = repo.insert_decision(_passed_decision("c1", "AAPL"))

    _runner(repo, broker).reconcile_on_startup()

    assert repo._decisions[decision_id]["risk_check_details"] == "decision_orphaned"


def test_orphan_skipped_when_matching_broker_order_exists() -> None:
    repo = FakeRepository()
    broker = InMemoryBrokerGateway()
    decision_id = repo.insert_decision(_passed_decision("c1", "AAPL"))
    broker.set_open_orders([
        OpenOrder(ibkr_order_id=1, ticker="AAPL", exchange_code="XNAS",
                  leg_role="entry", status="submitted", oca_group="te-1"),
    ])

    _runner(repo, broker).reconcile_on_startup()

    # A live broker order exists for the instrument -> leave for callbacks.
    assert repo._decisions[decision_id]["risk_check_details"] == "admitted"


def test_decision_with_executions_not_orphaned() -> None:
    repo = FakeRepository()
    broker = InMemoryBrokerGateway()
    decision_id = repo.insert_decision(_passed_decision("c1", "AAPL"))
    repo.insert_execution_leg(
        decision_id=decision_id, leg_role="entry", ibkr_order_id=5,
        ibkr_oca_group="te-1", status="submitted",
    )

    _runner(repo, broker).reconcile_on_startup()

    assert repo._decisions[decision_id]["risk_check_details"] == "admitted"


def test_rejected_decision_never_orphaned() -> None:
    repo = FakeRepository()
    broker = InMemoryBrokerGateway()
    rejected = _passed_decision("c1", "AAPL")
    rejected = DecisionRecord(**{**rejected.__dict__, "risk_check_passed": False,
                                 "risk_check_details": "not_in_watchlist"})
    decision_id = repo.insert_decision(rejected)

    _runner(repo, broker).reconcile_on_startup()

    assert repo._decisions[decision_id]["risk_check_details"] == "not_in_watchlist"
