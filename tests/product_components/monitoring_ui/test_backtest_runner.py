from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.monitoring_ui.backend.backtest_runner import BacktestRunCoordinator


def _fixed_now() -> datetime:
    return datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def _coordinator() -> BacktestRunCoordinator:
    return BacktestRunCoordinator(now_factory=_fixed_now)


def test_report_progress_visible_for_active_run() -> None:
    coordinator = _coordinator()
    coordinator._active_run_id = "bt_1"  # simulate an in-flight run

    coordinator.report_progress(run_id="bt_1", phase="prewarming", done=3, total=10, ticker="AAPL")

    progress = coordinator.current_progress("bt_1")
    assert progress is not None
    assert progress.phase == "prewarming"
    assert progress.done == 3
    assert progress.total == 10
    assert progress.current_ticker == "AAPL"
    assert progress.updated_at == _fixed_now()


def test_report_progress_ignored_for_non_active_run() -> None:
    coordinator = _coordinator()
    coordinator._active_run_id = "bt_1"

    coordinator.report_progress(run_id="bt_other", phase="prewarming", done=1, total=2, ticker="X")

    assert coordinator.current_progress("bt_other") is None
    assert coordinator.current_progress("bt_1") is None


def test_current_progress_scoped_to_run_id() -> None:
    coordinator = _coordinator()
    coordinator._active_run_id = "bt_1"
    coordinator.report_progress(run_id="bt_1", phase="simulating", done=0, total=0, ticker=None)

    assert coordinator.current_progress("bt_1") is not None
    assert coordinator.current_progress("bt_2") is None
