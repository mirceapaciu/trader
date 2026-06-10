from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.monitoring_ui.backend.models import (
    BacklogResponse,
    DeadLetterResponse,
    DependencyHealth,
    FilterQualityIncorrectlyRejectedItem,
    FilterQualityIncorrectlyRejectedResponse,
    FilterQualityRunSummary,
    FilterQualityStatusResponse,
    NewsFilterConfigPayload,
    ProvidersResponse,
    ProviderStatus,
    ThroughputResponse,
)
from src.product_components.monitoring_ui.backend.filter_quality_runner import FilterQualityRunCoordinator
from src.product_components.monitoring_ui.backend.service import FilterQualityRunAlreadyActive, MonitoringService
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


class FakeDataSource:
    def __init__(
        self,
        *,
        dependencies: list[DependencyHealth],
        providers: list[ProviderStatus],
    ) -> None:
        self.dependencies = dependencies
        self.providers = providers
        self.dead_letter_limit = 0
        self.dead_letter_offset = 0
        self.running_filter_quality_run: FilterQualityRunSummary | None = None
        self.last_filter_quality_run: FilterQualityRunSummary | None = None
        self.incorrectly_rejected_run_id: str | None = None
        self.stale_timeout_seconds: int | None = None
        self.test_filter = _filter_config("test_cfg", "test")
        self.production_filter = _filter_config("prod_cfg", "production")

    def check_dependencies(self) -> list[DependencyHealth]:
        return self.dependencies

    def list_providers(self) -> ProvidersResponse:
        return ProvidersResponse(providers=self.providers, generated_at=_now())

    def get_throughput(self, *, window: str) -> ThroughputResponse:
        return ThroughputResponse(window=window, buckets=[], generated_at=_now())

    def get_backlog(self) -> BacklogResponse:
        return BacklogResponse(pending_count=0, retrying_count=0, dead_letter_count=0, generated_at=_now())

    def list_dead_letters(self, *, limit: int, offset: int) -> DeadLetterResponse:
        self.dead_letter_limit = limit
        self.dead_letter_offset = offset
        return DeadLetterResponse(items=[], limit=limit, offset=offset, generated_at=_now())

    def get_filter_quality_status(self) -> FilterQualityStatusResponse:
        return FilterQualityStatusResponse(
            running_run=self.running_filter_quality_run,
            last_run=self.last_filter_quality_run,
            generated_at=_now(),
        )

    def list_filter_quality_incorrectly_rejected(
        self,
        *,
        run_id: str,
    ) -> FilterQualityIncorrectlyRejectedResponse:
        self.incorrectly_rejected_run_id = run_id
        return FilterQualityIncorrectlyRejectedResponse(
            run_id=run_id,
            items=[
                FilterQualityIncorrectlyRejectedItem(
                    assessment_id="fqa_1",
                    run_id=run_id,
                    article_id="article_1",
                    headline="Revenue outlook improves",
                    url="https://example.test/article",
                    source="example",
                    published_at=_now(),
                    production_filter_outcome="accepted",
                    simulation_filter_outcome="rejected",
                    rejection_reason_code="rejected_not_relevant",
                    probable_cause="keyword_gap",
                    improvement_suggestion="Add revenue outlook language to include keywords.",
                    rationale="The article contains material financial guidance.",
                    classification_confidence=0.91,
                    suggestion_json={},
                    evaluated_at=_now(),
                )
            ],
            generated_at=_now(),
        )

    def get_running_filter_quality_run(self) -> FilterQualityRunSummary | None:
        return self.running_filter_quality_run

    def mark_stale_filter_quality_runs_failed(self, *, timeout_seconds: int) -> int:
        self.stale_timeout_seconds = timeout_seconds
        return 0

    def get_production_filter_config(self) -> NewsFilterConfigPayload:
        return self.production_filter

    def get_test_filter_config(self) -> NewsFilterConfigPayload:
        return self.test_filter

    def save_test_filter_config(self, payload: NewsFilterConfigPayload) -> NewsFilterConfigPayload:
        self.test_filter = payload
        return payload

    def promote_test_filter_config(self) -> NewsFilterConfigPayload:
        self.production_filter = self.test_filter.model_copy(
            update={"config_role": "production", "filter_config_id": "prod_promoted"}
        )
        return self.production_filter


class FailingProviderDataSource(FakeDataSource):
    def list_providers(self) -> ProvidersResponse:
        raise RuntimeError("database unavailable")


class FakeFilterQualityRunner:
    def __init__(self, run_id: str = "fqe_started") -> None:
        self.run_id = run_id
        self.starts = 0

    def start_last_24h_run(self) -> str:
        self.starts += 1
        return self.run_id

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str:
        self.starts += 1
        return self.run_id


def test_health_is_healthy_when_dependencies_are_up_and_provider_data_is_fresh() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "healthy"
    assert health.liveness == "healthy"
    assert health.stale_data is False


def test_health_marks_readiness_unhealthy_when_dependency_fails() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="unhealthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now())],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "unhealthy"
    assert health.liveness == "healthy"
    assert health.active_incident_count == 1


def test_health_marks_liveness_unhealthy_when_provider_data_is_stale() -> None:
    data_source = FakeDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="healthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[ProviderStatus(source_key="finnhub", last_cycle_end_at=_now() - timedelta(minutes=10))],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "healthy"
    assert health.liveness == "unhealthy"
    assert health.stale_data is True


def test_health_degrades_when_provider_telemetry_query_fails() -> None:
    data_source = FailingProviderDataSource(
        dependencies=[
            DependencyHealth(name="postgres", kind="postgres", state="unhealthy", checked_at=_now()),
            DependencyHealth(name="redis", kind="redis", state="healthy", checked_at=_now()),
        ],
        providers=[],
    )
    service = MonitoringService(settings=_settings(), data_source=data_source)

    health = service.get_health()

    assert health.readiness == "unhealthy"
    assert health.liveness == "unhealthy"
    assert health.stale_data is True


def test_dead_letter_query_bounds_limit_and_offset() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    service.list_dead_letters(limit=1000, offset=-10)

    assert data_source.dead_letter_limit == 25
    assert data_source.dead_letter_offset == 0


def test_filter_quality_status_returns_running_and_last_terminal_run() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    data_source.running_filter_quality_run = _filter_quality_run("fqe_running", "running")
    data_source.last_filter_quality_run = _filter_quality_run("fqe_done", "completed")
    service = MonitoringService(settings=_settings(), data_source=data_source)

    status = service.get_filter_quality_status()

    assert data_source.stale_timeout_seconds == 1800
    assert status.running_run is not None
    assert status.running_run.run_id == "fqe_running"
    assert status.last_run is not None
    assert status.last_run.run_id == "fqe_done"


def test_list_filter_quality_incorrectly_rejected_delegates_to_data_source() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source)

    response = service.list_filter_quality_incorrectly_rejected(run_id="fqe_done")

    assert data_source.incorrectly_rejected_run_id == "fqe_done"
    assert response.run_id == "fqe_done"
    assert response.items[0].probable_cause == "keyword_gap"
    assert response.items[0].improvement_suggestion == "Add revenue outlook language to include keywords."


def test_start_filter_quality_run_returns_running_when_no_active_run_exists() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    runner = FakeFilterQualityRunner(run_id="fqe_new")
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=runner)

    response = service.start_filter_quality_run()

    assert response.run_id == "fqe_new"
    assert response.status == "running"
    assert runner.starts == 1
    assert data_source.stale_timeout_seconds == 1800


def test_start_filter_quality_run_raises_when_run_is_already_running() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    data_source.running_filter_quality_run = _filter_quality_run("fqe_active", "running")
    runner = FakeFilterQualityRunner()
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=runner)

    try:
        service.start_filter_quality_run()
    except FilterQualityRunAlreadyActive as exc:
        assert exc.run_id == "fqe_active"
    else:
        raise AssertionError("expected FilterQualityRunAlreadyActive")
    assert runner.starts == 0


def test_filter_config_workflow_delegates_to_data_source() -> None:
    data_source = FakeDataSource(dependencies=[], providers=[])
    service = MonitoringService(settings=_settings(), data_source=data_source, filter_quality_runner=FakeFilterQualityRunner())

    test_filter = service.get_test_filter_config()
    saved = service.save_test_filter_config(test_filter.model_copy(update={"include_keywords": ["guidance", "outlook"]}))
    simulation = service.start_test_filter_simulation()
    production = service.promote_test_filter_config()

    assert saved.include_keywords == ["guidance", "outlook"]
    assert simulation.status == "running"
    assert production.config_role == "production"


def test_filter_quality_coordinator_builds_last_24h_params_with_accepted_audit_disabled() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    coordinator = FilterQualityRunCoordinator(
        run_id_factory=lambda: "fqe_ui",
        now_factory=lambda: now,
    )

    params = coordinator._build_last_24h_params()

    assert params.run_id == "fqe_ui"
    assert params.news_window_start_at == now - timedelta(hours=24)
    assert params.news_window_end_at == now
    assert params.accepted_audit_enabled is False
    assert params.accepted_audit_sample_size is None
    assert params.run_note == "Started from Monitoring UI"


def _settings() -> MonitoringUiSettings:
    return MonitoringUiSettings(
        ui_port=8080,
        ui_api_base_url="http://localhost:8080/api",
        ui_refresh_interval_seconds=15,
        ui_provider_refresh_interval_seconds=10,
        ui_alerts_refresh_interval_seconds=20,
        ui_query_timeout_seconds=5,
        ui_stale_data_ttl_seconds=120,
        ui_default_time_window="1h",
        ui_export_max_rows=25,
        newsfetcher_db_schema="news_fetcher",
        filter_quality_db_schema="filter_quality_evaluator",
        filter_quality_run_timeout_seconds=1800,
        queue_url="redis://127.0.0.1:6379/0",
        news_raw_queue="news_raw_queue",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _filter_quality_run(run_id: str, status: str) -> FilterQualityRunSummary:
    return FilterQualityRunSummary(
        run_id=run_id,
        status=status,
        news_window_start_at=_now() - timedelta(hours=24),
        news_window_end_at=_now(),
        created_at=_now(),
        started_at=_now(),
        finished_at=_now() if status != "running" else None,
        error_code=None,
        rejection_precision_proxy=0.9,
        incorrectly_accepted_rate_estimate=None,
        dataset_input_count=12,
        dataset_rejected_count=7,
        dataset_accepted_count=5,
        rejected_items_evaluated=7,
        accepted_items_sampled=0,
        correctly_rejected_count=6,
        incorrectly_rejected_count=1,
        correctly_accepted_count=0,
        incorrectly_accepted_count=0,
        item_failed_count=0,
        item_error_codes={},
        total_filter_quality=0.9,
        total_correct_count=11,
        assumed_correct_accepted_count=5,
        evaluation_subject="simulation",
        summary_json={},
        recommendation_summary_md="",
    )


def _filter_config(config_id: str, role: str) -> NewsFilterConfigPayload:
    return NewsFilterConfigPayload(
        filter_config_id=config_id,
        config_name=f"{role} filter",
        config_role=role,
        status="active",
        include_keywords=["guidance"],
        exclude_keywords=[],
        watchlist_tickers=["AAPL"],
        dedupe_algorithm="rapidfuzz_ratio",
        dedupe_similarity_threshold=0.9,
        dedupe_lookback_hours=24,
    )
