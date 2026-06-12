from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.product_components.monitoring_ui.backend.app import _local_dev_origin_regex, create_app
from src.product_components.monitoring_ui.backend.models import ProvidersResponse, ProviderStatus, ThroughputResponse
from src.product_components.monitoring_ui.backend.repository import _throughput_granularity_for_window
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


def test_local_dev_origin_regex_allows_localhost_with_any_port() -> None:
    pattern = re.compile(_local_dev_origin_regex())

    assert pattern.match("http://localhost:5173")
    assert pattern.match("http://127.0.0.1:5174")
    assert pattern.match("https://localhost:8443")
    assert not pattern.match("http://example.com:5173")


class FakeMonitoringDataSource:
    def __init__(self, **_: object) -> None:
        self.window: str | None = None
        self.start_at: datetime | None = None
        self.end_at: datetime | None = None

    def bootstrap_news_schema(self, **_: object) -> None:
        return None

    def check_dependencies(self) -> list[object]:
        return []

    def list_providers(self):
        return ProvidersResponse(
            providers=[
                ProviderStatus(
                    source_key="finnhub",
                    last_cycle_end_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
                    last_non_zero_fetch_at=datetime(2026, 6, 12, 9, 45, tzinfo=timezone.utc),
                )
            ],
            generated_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        )

    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        self.window = window
        self.start_at = start_at
        self.end_at = end_at
        return ThroughputResponse(
            window=window,
            granularity=_throughput_granularity_for_window(window),
            window_start_at=start_at or datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
            window_end_at=end_at or datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
            buckets=[],
            generated_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        )

    def get_backlog(self):
        raise AssertionError("get_backlog should not be called in throughput endpoint tests")

    def list_dead_letters(self, *, limit: int, offset: int):
        raise AssertionError("list_dead_letters should not be called in throughput endpoint tests")

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int:
        return 0

    def get_filter_quality_status(self):
        raise AssertionError("get_filter_quality_status should not be called in throughput endpoint tests")

    def list_filter_quality_incorrectly_rejected(self, *, run_id: str):
        raise AssertionError("list_filter_quality_incorrectly_rejected should not be called in throughput endpoint tests")

    def get_production_filter_config(self):
        raise AssertionError("get_production_filter_config should not be called in throughput endpoint tests")

    def get_test_filter_config(self):
        raise AssertionError("get_test_filter_config should not be called in throughput endpoint tests")

    def save_test_filter_config(self, payload):
        raise AssertionError("save_test_filter_config should not be called in throughput endpoint tests")

    def promote_test_filter_config(self):
        raise AssertionError("promote_test_filter_config should not be called in throughput endpoint tests")

    def get_running_filter_quality_run(self):
        return None


class FakeFilterQualityRunner:
    def start_last_24h_run(self) -> str:
        return "unused"

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str:
        return "unused"


def test_throughput_endpoint_accepts_preset_window(monkeypatch) -> None:
    data_source = FakeMonitoringDataSource()
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/metrics/throughput", params={"window": "1d"})

    assert response.status_code == 200
    assert response.json()["window"] == "1d"
    assert data_source.window == "1d"


def test_throughput_endpoint_accepts_custom_range(monkeypatch) -> None:
    data_source = FakeMonitoringDataSource()
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get(
        "/api/metrics/throughput",
        params={"window": "7d", "start_at": "2026-06-12T08:00:00Z", "end_at": "2026-06-12T10:00:00Z"},
    )

    assert response.status_code == 200
    assert response.json()["window"] == "custom"
    assert response.json()["granularity"] == "hour"
    assert data_source.window == "7d"
    assert data_source.start_at == datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)
    assert data_source.end_at == datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)


def test_throughput_endpoint_rejects_invalid_window(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FakeMonitoringDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/metrics/throughput", params={"window": "30m"})

    assert response.status_code == 422
    assert "Unsupported throughput window" in response.json()["detail"]


def test_throughput_endpoint_accepts_30d_preset_window(monkeypatch) -> None:
    data_source = FakeMonitoringDataSource()
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/metrics/throughput", params={"window": "30d"})

    assert response.status_code == 200
    assert response.json()["window"] == "30d"
    assert data_source.window == "30d"


def test_providers_endpoint_includes_last_non_zero_fetch_timestamp(monkeypatch) -> None:
    data_source = FakeMonitoringDataSource()
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/providers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["providers"][0]["last_non_zero_fetch_at"] == "2026-06-12T09:45:00Z"


def _settings() -> MonitoringUiSettings:
    return MonitoringUiSettings(
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
        filter_quality_run_timeout_seconds=1800,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
    )
