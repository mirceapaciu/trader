from __future__ import annotations

import os

from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


def test_settings_include_redis_password_in_queue_url(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "change_me")

    settings = MonitoringUiSettings.from_env()

    assert settings.queue_url == "redis://:change_me@127.0.0.1:6379/0"


def test_settings_do_not_override_existing_credentials(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_URL", "redis://:already_set@127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "change_me")

    settings = MonitoringUiSettings.from_env()

    assert settings.queue_url == "redis://:already_set@127.0.0.1:6379/0"


def test_settings_load_thesis_builder_monitoring_values(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_BUILDER_DB_SCHEMA", "thesis_builder_test")
    monkeypatch.setenv("THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES", "90")

    settings = MonitoringUiSettings.from_env()

    assert settings.thesis_builder_db_schema == "thesis_builder_test"
    assert settings.thesis_builder_evidence_collection_max_minutes == 90
