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
