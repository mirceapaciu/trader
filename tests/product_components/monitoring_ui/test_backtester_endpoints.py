from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.product_components.monitoring_ui.backend.app import create_app
from src.product_components.monitoring_ui.backend.backtest_run_request import BacktestRunRequest
from src.product_components.monitoring_ui.backend.backtest_runner import BacktestProgress
from src.product_components.monitoring_ui.backend.repository import (
    BacktestEquityRow,
    BacktestRunRow,
    BacktestTradeRow,
    BacktesterTablesUnavailable,
)
from src.product_components.monitoring_ui.backend.service import (
    BacktestRunAlreadyActive,
    InvalidThroughputWindow,
    MonitoringService,
)
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


def _now() -> datetime:
    return datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def _run_row(
    *,
    run_id: str = "bt_1",
    status: str = "completed",
    timing_scenario: str = "ideal",
    summary_json: dict | None = None,
    llm_token_budget_limit: int | None = None,
    llm_tokens_used: int | None = None,
    budget_exhausted: bool | None = None,
    analysis_coverage_until_at: datetime | None = None,
) -> BacktestRunRow:
    return BacktestRunRow(
        run_id=run_id,
        status=status,
        window_start_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        window_end_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        mode="replay",
        timing_scenario=timing_scenario,
        card_population="all",
        strategies_requested=["sentiment_momentum"],
        initial_capital=10000.0,
        net_pnl=123.45,
        total_return=0.0123,
        win_rate=0.6,
        profit_factor=1.8,
        max_drawdown=0.05,
        created_at=datetime(2026, 6, 27, 11, 0, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 27, 11, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 27, 11, 30, tzinfo=timezone.utc) if status != "running" else None,
        error_code=None,
        gross_profit=200.0,
        gross_loss=-76.55,
        total_commission=4.0,
        total_slippage=2.0,
        avg_win=50.0,
        avg_loss=-25.0,
        expectancy=12.3,
        max_drawdown_duration_seconds=3600.0,
        sharpe_ratio=1.1,
        exposure_fraction=0.3,
        signal_accuracy=0.55,
        cards_considered=10,
        cards_in_population=8,
        cards_live_executable=6,
        cards_skipped_no_price=1,
        trades_opened=6,
        trades_closed=5,
        trades_risk_blocked=1,
        llm_model=None,
        avg_news_fetch_delay_seconds=12.0,
        p95_news_fetch_delay_seconds=30.0,
        max_news_fetch_delay_seconds=45.0,
        avg_thesis_build_delay_seconds=8.0,
        p95_thesis_build_delay_seconds=20.0,
        max_thesis_build_delay_seconds=25.0,
        avg_total_pipeline_delay_seconds=20.0,
        p95_total_pipeline_delay_seconds=50.0,
        max_total_pipeline_delay_seconds=70.0,
        pnl_gap=5.0,
        win_rate_gap=0.02,
        trades_flipped_by_delay=2,
        llm_token_budget_limit=llm_token_budget_limit,
        llm_tokens_used=llm_tokens_used,
        budget_exhausted=budget_exhausted,
        analysis_coverage_until_at=analysis_coverage_until_at,
        summary_json=summary_json or {},
    )


_SUMMARY = {
    "profit_factor_undefined": False,
    "by_strategy": {
        "sentiment_momentum": {
            "trades_opened": 6,
            "trades_closed": 5,
            "trades_risk_blocked": 1,
            "net_pnl": 123.45,
            "gross_profit": 200.0,
            "gross_loss": -76.55,
            "win_rate": 0.6,
            "avg_win": 50.0,
            "avg_loss": -25.0,
            "profit_factor": 1.8,
            "expectancy": 12.3,
        }
    },
    "by_card_status": {
        "approved": {"trades_opened": 4, "net_pnl": 100.0, "win_rate": 0.7},
        "rejected": {"trades_opened": 2, "net_pnl": 23.45, "win_rate": 0.5},
        "card_was_live_expired": {"trades_opened": 1, "net_pnl": -5.0},
        "card_unexpired_at_entry": {"trades_opened": 5, "net_pnl": 128.45},
    },
}


class FakeBacktestDataSource:
    def __init__(self) -> None:
        self.runs: list[BacktestRunRow] = []
        self.active: BacktestRunRow | None = None
        self.run_by_id: dict[str, BacktestRunRow] = {}
        self.trades: list[BacktestTradeRow] = []
        self.equity: list[BacktestEquityRow] = []
        self.unavailable = False
        self.last_trade_filters: dict | None = None
        self.window_start_at: datetime | None = None

    def list_backtest_runs(self, *, window_start_at: datetime):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        self.window_start_at = window_start_at
        return list(self.runs)

    def get_active_backtest_run(self):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        return self.active

    def get_backtest_run(self, *, run_id: str):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        return self.run_by_id.get(run_id)

    def count_backtest_trades(self, *, run_id, timing_scenario=None, strategy=None, exit_reason=None, card_status=None):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        return len(self.trades)

    def list_backtest_trades(self, *, run_id, timing_scenario=None, strategy=None, exit_reason=None, card_status=None, limit, offset):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        self.last_trade_filters = {
            "timing_scenario": timing_scenario,
            "strategy": strategy,
            "exit_reason": exit_reason,
            "card_status": card_status,
            "limit": limit,
            "offset": offset,
        }
        return list(self.trades)

    def list_backtest_equity_points(self, *, run_id: str):
        if self.unavailable:
            raise BacktesterTablesUnavailable("missing")
        return list(self.equity)


class FakeBacktestRunner:
    def __init__(self) -> None:
        self.last_request: BacktestRunRequest | None = None
        self.raise_already_active = False
        self.progress: BacktestProgress | None = None

    def start_run(self, request: BacktestRunRequest) -> str:
        if self.raise_already_active:
            raise BacktestRunAlreadyActive("bt_active")
        self.last_request = request
        return "bt_new"

    def current_progress(self, run_id: str) -> BacktestProgress | None:
        if self.progress is not None and self.progress.run_id == run_id:
            return self.progress
        return None


def _settings() -> MonitoringUiSettings:
    return MonitoringUiSettings(
        ui_host="127.0.0.1",
        ui_port=8080,
        ui_api_base_url="http://localhost:8080/api",
        ui_refresh_interval_seconds=15,
        ui_provider_refresh_interval_seconds=10,
        ui_alerts_refresh_interval_seconds=20,
        ui_query_timeout_seconds=5,
        ui_stale_data_ttl_seconds=120,
        ui_default_time_window="1d",
        ui_export_max_rows=25,
        newsfetcher_db_schema="news_fetcher",
        filter_quality_db_schema="filter_quality_evaluator",
        backtester_db_schema="backtester",
        ui_backtest_refresh_interval_seconds=15,
        shared_db_schema="shared",
        watchlist_table="t_watchlist_tickers",
        ui_thesis_builder_stall_threshold_seconds=600,
        filter_quality_run_timeout_seconds=1800,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        failed_messages_dlq="failed_messages_dlq",
        reprocess_command_queue="reprocess_command_queue",
        massive_api_key="",
        massive_api_base_url="https://api.polygon.io",
        alpha_vantage_api_key="",
        openfigi_api_key="",
        instrument_lookup_cache_ttl_seconds=21600,
        instrument_alias_cache_ttl_seconds=86400,
        instrument_lookup_provider_debounce_ms=300,
    )


def _service(data_source: FakeBacktestDataSource, runner: FakeBacktestRunner | None = None) -> MonitoringService:
    return MonitoringService(
        settings=_settings(),
        data_source=data_source,
        backtest_runner=runner,
    )


# --- service projection logic -------------------------------------------------


def test_get_backtests_returns_runs_and_active_run() -> None:
    ds = FakeBacktestDataSource()
    ds.runs = [_run_row(run_id="bt_1"), _run_row(run_id="bt_2", status="running")]
    ds.active = _run_row(run_id="bt_2", status="running")
    result = _service(ds).get_backtests(window="7d")
    assert result.available is True
    assert result.window == "7d"
    assert [r.run_id for r in result.runs] == ["bt_1", "bt_2"]
    assert result.active_run is not None
    assert result.active_run.run_id == "bt_2"


def test_get_backtests_attaches_live_progress_to_active_run() -> None:
    ds = FakeBacktestDataSource()
    ds.runs = [_run_row(run_id="bt_2", status="running")]
    ds.active = _run_row(run_id="bt_2", status="running")
    runner = FakeBacktestRunner()
    runner.progress = BacktestProgress(
        run_id="bt_2",
        phase="prewarming",
        done=12,
        total=50,
        current_ticker="AAPL",
        updated_at=datetime(2026, 6, 27, 11, 5, tzinfo=timezone.utc),
    )

    result = _service(ds, runner).get_backtests(window="7d")

    assert result.active_run is not None
    assert result.active_run.progress is not None
    assert result.active_run.progress.phase == "prewarming"
    assert result.active_run.progress.done == 12
    assert result.active_run.progress.total == 50
    assert result.active_run.progress.current_ticker == "AAPL"
    # Non-active runs in the list carry no live progress.
    assert all(run.progress is None for run in result.runs)


def test_get_backtests_omits_progress_for_mismatched_run() -> None:
    ds = FakeBacktestDataSource()
    ds.active = _run_row(run_id="bt_2", status="running")
    runner = FakeBacktestRunner()
    runner.progress = BacktestProgress(
        run_id="some_other_run",
        phase="simulating",
        done=0,
        total=0,
        current_ticker=None,
        updated_at=datetime(2026, 6, 27, 11, 5, tzinfo=timezone.utc),
    )

    result = _service(ds, runner).get_backtests(window="7d")

    assert result.active_run is not None
    assert result.active_run.progress is None


def test_get_backtests_rejects_invalid_window() -> None:
    ds = FakeBacktestDataSource()
    with pytest.raises(InvalidThroughputWindow):
        _service(ds).get_backtests(window="42m")


def test_get_backtests_degrades_when_tables_missing() -> None:
    ds = FakeBacktestDataSource()
    ds.unavailable = True
    result = _service(ds).get_backtests(window="1d")
    assert result.available is False
    assert result.runs == []


def test_detail_projects_summary_json_into_per_strategy_and_card_status() -> None:
    ds = FakeBacktestDataSource()
    run = _run_row(run_id="bt_1", summary_json=_SUMMARY)
    ds.run_by_id["bt_1"] = run
    detail = _service(ds).get_backtest_detail(run_id="bt_1")
    assert detail is not None
    assert [m.strategy for m in detail.per_strategy] == ["sentiment_momentum"]
    assert detail.per_strategy[0].net_pnl == 123.45
    buckets = [m.bucket for m in detail.card_status_breakdown]
    assert buckets == ["approved", "rejected", "card_was_live_expired", "card_unexpired_at_entry"]
    assert detail.metrics.cards_considered == 10


def test_list_flags_budget_exhausted_run_from_row_columns() -> None:
    ds = FakeBacktestDataSource()
    coverage = datetime(2026, 6, 20, 13, tzinfo=timezone.utc)
    ds.runs = [
        _run_row(run_id="bt_partial", budget_exhausted=True, analysis_coverage_until_at=coverage),
        _run_row(run_id="bt_full", budget_exhausted=False),
        _run_row(run_id="bt_replay"),  # legacy/replay row: nulls
    ]
    runs = {r.run_id: r for r in _service(ds).get_backtests(window="7d").runs}
    assert runs["bt_partial"].budget_exhausted is True
    assert runs["bt_partial"].analysis_coverage_until_at == coverage
    assert runs["bt_full"].budget_exhausted is False
    # A legacy/replay row (null columns) never claims partial coverage.
    assert runs["bt_replay"].budget_exhausted is None
    assert runs["bt_replay"].analysis_coverage_until_at is None


def test_detail_projects_regeneration_budget_and_coverage() -> None:
    ds = FakeBacktestDataSource()
    coverage_iso = "2026-06-20T13:00:00+00:00"
    summary = {
        "regeneration": {
            "articles_found": 10,
            "analyses_created": 4,
            "cards_created": 1,
            "budget_exhausted": True,
            "llm_tokens_used": 12000,
            "llm_token_budget_limit": 12345,
            "analysis_coverage_until_at": coverage_iso,
            "analysis_coverage_fraction": 0.5,
        }
    }
    ds.run_by_id["bt_1"] = _run_row(run_id="bt_1", summary_json=summary)
    detail = _service(ds).get_backtest_detail(run_id="bt_1")
    regen = detail.regeneration
    assert regen is not None
    assert regen.budget_exhausted is True
    assert regen.llm_tokens_used == 12000
    assert regen.llm_token_budget_limit == 12345
    assert regen.analysis_coverage_until_at == datetime(2026, 6, 20, 13, tzinfo=timezone.utc)
    assert regen.analysis_coverage_fraction == 0.5


def test_detail_gap_gated_by_timing_scenario_both() -> None:
    ds = FakeBacktestDataSource()
    ds.run_by_id["ideal"] = _run_row(run_id="ideal", timing_scenario="ideal", summary_json=_SUMMARY)
    ds.run_by_id["both"] = _run_row(run_id="both", timing_scenario="both", summary_json=_SUMMARY)
    assert _service(ds).get_backtest_detail(run_id="ideal").gap is None
    gap = _service(ds).get_backtest_detail(run_id="both").gap
    assert gap is not None
    assert gap.pnl_gap == 5.0
    assert gap.trades_flipped_by_delay == 2


def test_detail_empty_summary_returns_no_breakdowns() -> None:
    ds = FakeBacktestDataSource()
    ds.run_by_id["bt_1"] = _run_row(run_id="bt_1", summary_json={})
    detail = _service(ds).get_backtest_detail(run_id="bt_1")
    assert detail.per_strategy == []
    assert detail.card_status_breakdown == []


def test_detail_returns_none_when_missing() -> None:
    ds = FakeBacktestDataSource()
    assert _service(ds).get_backtest_detail(run_id="nope") is None


def test_equity_grouped_into_series_per_scenario() -> None:
    ds = FakeBacktestDataSource()
    ds.equity = [
        BacktestEquityRow(timing_scenario="ideal", as_of=datetime(2026, 6, 20, 9, tzinfo=timezone.utc), equity=10000.0, open_positions=0),
        BacktestEquityRow(timing_scenario="ideal", as_of=datetime(2026, 6, 20, 10, tzinfo=timezone.utc), equity=10100.0, open_positions=1),
        BacktestEquityRow(timing_scenario="actual", as_of=datetime(2026, 6, 20, 9, tzinfo=timezone.utc), equity=10000.0, open_positions=0),
    ]
    result = _service(ds).get_backtest_equity(run_id="bt_1")
    scenarios = {s.timing_scenario: s for s in result.series}
    assert set(scenarios) == {"ideal", "actual"}
    assert len(scenarios["ideal"].points) == 2


def test_start_backtest_run_rejects_inverted_window() -> None:
    ds = FakeBacktestDataSource()
    from src.product_components.monitoring_ui.backend.models import BacktestStartRunRequest
    from src.product_components.monitoring_ui.backend.service import InvalidBacktestWindow

    payload = BacktestStartRunRequest(
        window_start_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        window_end_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )
    with pytest.raises(InvalidBacktestWindow):
        _service(ds, FakeBacktestRunner()).start_backtest_run(payload)


# --- app routes ---------------------------------------------------------------


def _client(monkeypatch, data_source: FakeBacktestDataSource, runner: FakeBacktestRunner) -> TestClient:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: object(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.BacktestRunCoordinator",
        lambda: runner,
    )
    return TestClient(create_app(settings=_settings()))


def test_route_list_backtests(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    ds.runs = [_run_row(run_id="bt_1")]
    client = _client(monkeypatch, ds, FakeBacktestRunner())
    response = client.get("/api/backtests", params={"window": "1d"})
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1d"
    assert body["runs"][0]["run_id"] == "bt_1"


def test_route_detail_404(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    client = _client(monkeypatch, ds, FakeBacktestRunner())
    response = client.get("/api/backtests/missing")
    assert response.status_code == 404


def test_route_trades_passes_filters(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    ds.trades = [
        BacktestTradeRow(
            trade_id="t1", ticker="AAPL", exchange_code="XNAS", strategy="sentiment_momentum",
            direction="buy", entry_timing_scenario="ideal", entry_at=_now(), entry_price=100.0,
            exit_at=_now(), exit_price=103.0, net_pnl=3.0, return_pct=0.03, exit_reason="take_profit",
            risk_block_rule=None, news_fetch_delay_seconds=1.0, thesis_build_delay_seconds=2.0,
            total_pipeline_delay_seconds=3.0, card_decision_state="approved", card_was_live_expired=False,
        )
    ]
    client = _client(monkeypatch, ds, FakeBacktestRunner())
    response = client.get("/api/backtests/bt_1/trades", params={"strategy": "sentiment_momentum", "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["trades"][0]["trade_id"] == "t1"
    assert ds.last_trade_filters["strategy"] == "sentiment_momentum"


def test_route_start_run_returns_202(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    runner = FakeBacktestRunner()
    client = _client(monkeypatch, ds, runner)
    response = client.post(
        "/api/backtests",
        json={
            "window_start_at": "2026-06-20T00:00:00Z",
            "window_end_at": "2026-06-21T00:00:00Z",
        },
    )
    assert response.status_code == 202
    assert response.json() == {"run_id": "bt_new", "status": "running"}
    assert runner.last_request is not None
    assert runner.last_request.window_start_at == datetime(2026, 6, 20, tzinfo=timezone.utc)
    assert runner.last_request.window_end_at == datetime(2026, 6, 21, 23, 59, 59, tzinfo=timezone.utc)


def test_route_start_run_returns_409_when_active(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    runner = FakeBacktestRunner()
    runner.raise_already_active = True
    client = _client(monkeypatch, ds, runner)
    response = client.post(
        "/api/backtests",
        json={
            "window_start_at": "2026-06-20T00:00:00Z",
            "window_end_at": "2026-06-21T00:00:00Z",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"run_id": "bt_active", "status": "running", "message": "already running"}


def test_route_start_run_returns_422_for_inverted_window(monkeypatch) -> None:
    ds = FakeBacktestDataSource()
    client = _client(monkeypatch, ds, FakeBacktestRunner())
    response = client.post(
        "/api/backtests",
        json={
            "window_start_at": "2026-06-21T00:00:00Z",
            "window_end_at": "2026-06-20T00:00:00Z",
        },
    )
    assert response.status_code == 422


# --- coordinator --------------------------------------------------------------


def test_coordinator_raises_already_active_when_busy() -> None:
    from src.product_components.backtester.settings import BacktesterSettings
    from src.product_components.monitoring_ui.backend.backtest_runner import BacktestRunCoordinator

    coordinator = BacktestRunCoordinator(
        settings_factory=lambda: BacktesterSettings.from_env(),
        run_id_factory=lambda: "bt_fixed",
    )
    # Force a busy state without launching a real background thread.
    coordinator._active_run_id = "bt_existing"  # noqa: SLF001
    request = BacktestRunRequest(
        window_start_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        window_end_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
    )
    with pytest.raises(BacktestRunAlreadyActive):
        coordinator.start_run(request)


def test_coordinator_rejects_inverted_window() -> None:
    from src.product_components.backtester.settings import BacktesterSettings
    from src.product_components.monitoring_ui.backend.backtest_runner import BacktestRunCoordinator

    coordinator = BacktestRunCoordinator(
        settings_factory=lambda: BacktesterSettings.from_env(),
        run_id_factory=lambda: "bt_fixed",
    )
    request = BacktestRunRequest(
        window_start_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        window_end_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError):
        coordinator.start_run(request)
