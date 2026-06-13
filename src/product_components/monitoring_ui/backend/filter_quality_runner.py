from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.product_components.filter_quality_evaluator.models import FilterQualityRunParams
from src.product_components.filter_quality_evaluator.repository import FilterQualityRepository
from src.product_components.filter_quality_evaluator.service import (
    FilterQualityEvaluatorService,
    new_run_id,
)
from src.product_components.filter_quality_evaluator.settings import FilterQualityEvaluatorSettings
from src.product_components.news_fetcher.settings import NewsFetcherSettings

from .service import FilterQualityRunAlreadyActive

LOGGER = logging.getLogger(__name__)


class FilterQualityRunCoordinator:
    def __init__(
        self,
        *,
        settings_factory: Callable[[], FilterQualityEvaluatorSettings] = FilterQualityEvaluatorSettings.from_env,
        news_settings_factory: Callable[[], NewsFetcherSettings] = NewsFetcherSettings.from_env,
        run_id_factory: Callable[[], str] = new_run_id,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings_factory = settings_factory
        self._news_settings_factory = news_settings_factory
        self._run_id_factory = run_id_factory
        self._now_factory = now_factory or _utc_now
        self._lock = threading.Lock()
        self._active_run_id: str | None = None

    def start_last_24h_run(self, *, accepted_audit_enabled: bool = False) -> str:
        params = self._build_last_24h_params(accepted_audit_enabled=accepted_audit_enabled)
        return self._start(params)

    def start_last_24h_run_with_snapshot(self, snapshot: dict) -> str:
        params = self._build_last_24h_params(filter_config_snapshot_json=snapshot)
        return self._start(params)

    def _start(self, params: FilterQualityRunParams) -> str:
        with self._lock:
            if self._active_run_id is not None:
                raise FilterQualityRunAlreadyActive(self._active_run_id)
            self._active_run_id = params.run_id

        thread = threading.Thread(
            target=self._run_in_background,
            args=(params,),
            name=f"filter-quality-{params.run_id}",
            daemon=True,
        )
        thread.start()
        return params.run_id

    def _build_last_24h_params(
        self,
        filter_config_snapshot_json: dict | None = None,
        *,
        accepted_audit_enabled: bool = False,
    ) -> FilterQualityRunParams:
        now = self._now_factory()
        settings = self._settings_factory()
        return FilterQualityRunParams(
            run_id=self._run_id_factory(),
            news_window_start_at=now - timedelta(hours=24),
            news_window_end_at=now,
            filter_config_snapshot_json=filter_config_snapshot_json,
            run_note="Started from Monitoring UI",
            accepted_audit_enabled=accepted_audit_enabled,
            accepted_audit_sample_size=(
                settings.accepted_audit_sample_size if accepted_audit_enabled else None
            ),
        )

    def _run_in_background(self, params: FilterQualityRunParams) -> None:
        try:
            settings = self._settings_factory()
            repository = FilterQualityRepository(
                dsn=settings.postgres_dsn,
                evaluator_schema=settings.db_schema,
                news_schema=settings.newsfetcher_db_schema,
            )
            repository.bootstrap_schema(repo_root=_repo_root())
            FilterQualityEvaluatorService(
                settings=settings,
                news_settings=self._news_settings_factory(),
                repository=repository,
            ).run(params)
        except Exception:
            LOGGER.exception("filter quality evaluator run failed run_id=%s", params.run_id)
        finally:
            with self._lock:
                if self._active_run_id == params.run_id:
                    self._active_run_id = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]
