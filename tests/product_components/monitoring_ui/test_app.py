from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import psycopg
import redis

from src.product_components.monitoring_ui.backend.app import _local_dev_origin_regex, create_app
from src.product_components.monitoring_ui.backend.models import (
    FilterQualityIncorrectlyAcceptedResponse,
    ProvidersResponse,
    ProviderStatus,
    ThesisBuilderMetricsResponse,
    ThroughputResponse,
)
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
        self.incorrectly_accepted_run_id: str | None = None

    def bootstrap_news_schema(self, **_: object) -> None:
        return None

    def bootstrap_thesis_builder_schema(self, **_: object) -> None:
        return None

    def check_dependencies(self) -> list[object]:
        return []

    def list_providers(self):
        return ProvidersResponse(
            providers=[
                ProviderStatus(
                    source_key="finnhub",
                    last_cycle_end_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
                    last_fetch_attempt_at=datetime(2026, 6, 12, 9, 50, tzinfo=timezone.utc),
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

    def get_thesis_builder_metrics(
        self,
        *,
        window: str,
        evidence_collection_max_minutes: int,
    ) -> ThesisBuilderMetricsResponse:
        self.window = window
        return ThesisBuilderMetricsResponse(
            available=True,
            window=window,
            window_start_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
            window_end_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
            articles_processed_count=5,
            market_moving_articles_count=3,
            articles_included_in_cards_count=4,
            stale_articles_count=1,
            created_thesis_cards_count=2,
            pending_thesis_cards_count=1,
            missed_stale_thesis_cards_count=1,
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

    def list_filter_quality_incorrectly_accepted(self, *, run_id: str):
        self.incorrectly_accepted_run_id = run_id
        return FilterQualityIncorrectlyAcceptedResponse(run_id=run_id, items=[], generated_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc))

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
    def __init__(self) -> None:
        self.last_accepted_audit_enabled = False

    def start_last_24h_run(self, *, accepted_audit_enabled: bool = False) -> str:
        self.last_accepted_audit_enabled = accepted_audit_enabled
        return "unused"

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str:
        return "unused"


class FailingProvidersDataSource(FakeMonitoringDataSource):
    def list_providers(self):
        raise psycopg.OperationalError("db unavailable")


class FailingThroughputDataSource(FakeMonitoringDataSource):
    def get_throughput(
        self,
        *,
        window: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> ThroughputResponse:
        raise psycopg.OperationalError("db unavailable")


class FailingBacklogDataSource(FakeMonitoringDataSource):
    def get_backlog(self):
        raise redis.ConnectionError("redis unavailable")


class FailingDeadLetterDataSource(FakeMonitoringDataSource):
    def list_dead_letters(self, *, limit: int, offset: int):
        raise psycopg.OperationalError("db unavailable")


class FailingFilterQualityDataSource(FakeMonitoringDataSource):
    def get_filter_quality_status(self):
        raise psycopg.OperationalError("db unavailable")


class FailingWatchlistAdmin:
    def list_watchlist(self):
        return []

    def lookup(self, query: str, *, expand: bool = False):
        return ([], False)

    def add_watchlist_entry(self, entry):
        raise psycopg.OperationalError("db unavailable")

    def update_watchlist_entry(self, entry):
        raise psycopg.OperationalError("db unavailable")

    def discover_aliases(self, *, ticker: str, exchange_code: str, display_name: str | None):
        raise psycopg.OperationalError("db unavailable")

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str) -> None:
        raise psycopg.OperationalError("db unavailable")


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


def test_thesis_builder_metrics_endpoint_returns_metrics(monkeypatch) -> None:
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

    response = client.get("/api/thesis-builder/metrics", params={"window": "1h"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["window"] == "1h"
    assert payload["articles_processed_count"] == 5
    assert payload["missed_stale_thesis_cards_count"] == 1
    assert data_source.window == "1h"


def test_thesis_builder_metrics_endpoint_rejects_invalid_window(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FakeMonitoringDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/thesis-builder/metrics", params={"window": "12h"})

    assert response.status_code == 422
    assert "Unsupported thesis-builder metrics window" in response.json()["detail"]


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


def test_providers_endpoint_includes_last_fetch_attempt_timestamp(monkeypatch) -> None:
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
    assert payload["providers"][0]["last_fetch_attempt_at"] == "2026-06-12T09:50:00Z"


def test_incorrectly_accepted_endpoint_returns_items(monkeypatch) -> None:
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

    response = client.get("/api/filter-quality/runs/fqe_done/incorrectly-accepted")

    assert response.status_code == 200
    assert response.json()["run_id"] == "fqe_done"
    assert data_source.incorrectly_accepted_run_id == "fqe_done"


def test_start_filter_quality_endpoint_accepts_accepted_audit_flag(monkeypatch) -> None:
    data_source = FakeMonitoringDataSource()
    runner = FakeFilterQualityRunner()
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: data_source,
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: runner,
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.post("/api/filter-quality/runs", json={"accepted_audit_enabled": True})

    assert response.status_code == 202
    assert runner.last_accepted_audit_enabled is True


def test_providers_endpoint_returns_degraded_response_with_cors_headers(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FailingProvidersDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/providers", headers={"Origin": "http://127.0.0.1:5174"})

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["message"] == "Provider telemetry unavailable."
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"


def test_throughput_endpoint_returns_degraded_response_when_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FailingThroughputDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/metrics/throughput", params={"window": "1d"})

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["message"] == "Throughput data unavailable."
    assert response.json()["buckets"] == []


def test_backlog_endpoint_returns_degraded_response_when_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FailingBacklogDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/backlog")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["message"] == "Backlog data unavailable."


def test_dead_letter_endpoint_returns_degraded_response_when_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FailingDeadLetterDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/dead-letter", params={"limit": 25, "offset": 0})

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["message"] == "Dead-letter data unavailable."
    assert response.json()["items"] == []


def test_filter_quality_endpoint_returns_degraded_response_when_dependency_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FailingFilterQualityDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.get("/api/filter-quality")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["message"] == "Filter-quality data unavailable."
    assert response.json()["last_run"] is None


def test_watchlist_add_endpoint_returns_structured_503_when_storage_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.PostgresRedisMonitoringDataSource",
        lambda **kwargs: FakeMonitoringDataSource(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.SharedInstrumentLookupAdminService",
        lambda *args, **kwargs: FailingWatchlistAdmin(),
    )
    monkeypatch.setattr(
        "src.product_components.monitoring_ui.backend.app.FilterQualityRunCoordinator",
        lambda: FakeFilterQualityRunner(),
    )
    client = TestClient(create_app(settings=_settings()))

    response = client.post(
        "/api/watchlist",
        json={
            "ticker": "NVDA",
            "exchange_code": "XNAS",
            "display_name": "NVIDIA Corporation",
            "aliases": ["NVIDIA"],
            "source": "manual",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "watchlist storage unavailable"


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
        thesis_builder_db_schema="thesis_builder",
        shared_db_schema="shared",
        watchlist_table="t_watchlist_tickers",
        thesis_builder_evidence_collection_max_minutes=120,
        filter_quality_run_timeout_seconds=1800,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
        massive_api_key="",
        massive_api_base_url="https://api.polygon.io",
        alpha_vantage_api_key="",
        instrument_lookup_cache_ttl_seconds=21600,
        instrument_alias_cache_ttl_seconds=86400,
        instrument_lookup_provider_debounce_ms=300,
    )
