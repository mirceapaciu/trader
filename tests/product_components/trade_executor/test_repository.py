from datetime import date, datetime, timezone

import pytest

from src.product_components.trade_executor.repository import (
    PostgresTradeExecutorRepository,
    _decision_from_row,
    _position_from_row,
    _safe_identifier,
)


def test_safe_identifier_rejects_injection() -> None:
    with pytest.raises(ValueError):
        _safe_identifier("trade_executor; DROP TABLE x")
    assert _safe_identifier("trade_executor") == "trade_executor"


def test_construction_validates_schema() -> None:
    repo = PostgresTradeExecutorRepository(dsn="host=x", schema="trade_executor")
    assert repo._schema == "trade_executor"
    with pytest.raises(ValueError):
        PostgresTradeExecutorRepository(dsn="host=x", schema="bad-schema!")


def test_decision_row_mapper() -> None:
    row = {
        "id": 7,
        "thesis_card_id": "c1",
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "action": "buy",
        "quantity": 10,
        "order_type": "limit",
        "limit_price": 100.1,
        "entry_price": 100.0,
        "stop_price": 97.0,
        "take_profit_price": 106.0,
        "atr_20d": 2.0,
        "risk_amount_usd": 120.0,
        "confidence": 0.8,
        "signal_strength": None,
        "source_analysis_ids": [1, 2],
        "risk_check_passed": True,
        "risk_check_details": None,
        "decided_at": datetime(2026, 7, 3, tzinfo=timezone.utc),
    }
    rec = _decision_from_row(row)
    assert rec.id == 7
    assert rec.risk_check_passed is True
    assert rec.source_analysis_ids == [1, 2]


def test_position_row_mapper() -> None:
    row = {
        "id": 3,
        "thesis_card_id": "c1",
        "decision_id": 7,
        "ticker": "AAPL",
        "exchange_code": "XNAS",
        "side": "long",
        "quantity": 10,
        "avg_entry_price": 100.0,
        "stop_price": 97.0,
        "take_profit_price": 106.0,
        "time_exit_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "opened_at": datetime(2026, 7, 3, tzinfo=timezone.utc),
        "closed_at": None,
        "realized_pnl": None,
        "exit_reason": None,
    }
    rec = _position_from_row(row)
    assert rec.side == "long"
    assert rec.closed_at is None
    assert rec.time_exit_at.date() == date(2026, 7, 10)
