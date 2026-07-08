from __future__ import annotations

from pathlib import Path

from src.product_components.backtester import repository as repo_module
from src.product_components.backtester.repository import (
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
