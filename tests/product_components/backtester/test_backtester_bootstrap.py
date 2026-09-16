from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.product_components.backtester import repository as repo_module
from src.product_components.backtester.models import ExitReason, SimulatedTrade
from src.product_components.backtester.repository import (
    BacktesterRepository,
    backtester_schema_file,
    bootstrap_backtester_schema,
)


def _repo_root() -> Path:
    # tests/product_components/backtester/test_backtester_bootstrap.py -> repo root
    return Path(__file__).resolve().parents[3]


class _FakeCursor:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._sink.append(sql)


class _FakeConnection:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink
        self.autocommit = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._sink)

    def close(self) -> None:
        self.closed = True


def test_backtester_schema_file_points_at_component_ddl() -> None:
    path = backtester_schema_file(_repo_root())
    assert path.exists()
    assert path.parts[-3:] == ("backtester", "db", "schema.sql")


def test_bootstrap_backtester_schema_applies_schema_sql(monkeypatch) -> None:
    executed: list[str] = []
    fake = _FakeConnection(executed)
    monkeypatch.setattr(repo_module.psycopg, "connect", lambda dsn: fake)

    bootstrap_backtester_schema(dsn="host=x", repo_root=_repo_root())

    assert fake.autocommit is True
    assert fake.closed is True  # connection closed via contextlib.closing
    applied = "\n".join(executed)
    assert "SET lock_timeout" in executed[0]
    assert "SET statement_timeout" in executed[1]
    assert "CREATE SCHEMA IF NOT EXISTS backtester" in applied
    assert "t_backtest_runs" in applied
    assert "t_llm_analysis_cache" in applied
    assert "ADD COLUMN IF NOT EXISTS mfe_pct" in applied
    assert "ADD COLUMN IF NOT EXISTS horizon_returns_json" in applied
    assert "ADD COLUMN IF NOT EXISTS decision_at" in applied
    assert "ADD COLUMN IF NOT EXISTS decision_stage" in applied
    assert "ADD COLUMN IF NOT EXISTS decision_reason" in applied
    assert "ADD COLUMN IF NOT EXISTS decision_details_json" in applied
    assert "CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_decision_stage_reason" in applied


class _RecordingCursor:
    def __init__(self, sink: list[tuple[str, tuple | None]]) -> None:
        self._sink = sink

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._sink.append((sql, params))


class _RecordingConnection:
    def __init__(self, sink: list[tuple[str, tuple | None]]) -> None:
        self._sink = sink
        self.committed = False

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._sink)

    def commit(self) -> None:
        self.committed = True


def _blocked_trade(
    *, trade_id: str, details: dict | None, decision_stage: str | None
) -> SimulatedTrade:
    decided_at = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    return SimulatedTrade(
        trade_id=trade_id,
        run_id="bt-1",
        thesis_card_id=f"card-{trade_id}",
        ticker="AAPL",
        exchange_code="XNAS",
        strategy="event_driven",
        direction="buy",
        card_decision_state="approved",
        card_was_live_expired=False,
        entry_timing_scenario="actual",
        news_published_at=None,
        news_fetched_at=None,
        card_created_at=decided_at,
        news_fetch_delay_seconds=None,
        thesis_build_delay_seconds=None,
        total_pipeline_delay_seconds=None,
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
        risk_block_rule="size_below_one_share",
        holding_period_seconds=None,
        decision_at=decided_at if details is not None else None,
        decision_stage=decision_stage,
        decision_reason="size_below_one_share" if details is not None else None,
        decision_details_json=details,
    )


def test_insert_trades_persists_structured_details_and_legacy_nulls(monkeypatch) -> None:
    executions: list[tuple[str, tuple | None]] = []
    connection = _RecordingConnection(executions)
    repository = BacktesterRepository(
        dsn="",
        backtester_schema="backtester",
        market_data_schema="market_data",
        thesis_builder_schema="thesis_builder",
        shared_schema="shared",
    )
    monkeypatch.setattr(repository, "_connect", lambda: connection)
    details = {
        "schema_version": 1,
        "stage": "sizing",
        "reason": "size_below_one_share",
        "check_id": "sizing.minimum_quantity",
    }

    repository.insert_trades(
        run_id="bt-1",
        trades=[
            _blocked_trade(trade_id="structured", details=details, decision_stage="sizing"),
            _blocked_trade(trade_id="legacy", details=None, decision_stage=None),
        ],
    )

    assert connection.committed is True
    assert len(executions) == 2
    structured_params = executions[0][1]
    legacy_params = executions[1][1]
    assert structured_params is not None
    assert structured_params[28] == datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert structured_params[29:31] == ("sizing", "size_below_one_share")
    assert structured_params[31].obj == details
    assert legacy_params is not None
    assert legacy_params[28:32] == (None, None, None, None)
