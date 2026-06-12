from __future__ import annotations

from src.product_components.news_fetcher.settings import NewsFetcherSettings


def test_settings_include_redis_password_in_queue_url(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "change_me")

    settings = NewsFetcherSettings.from_env()

    assert settings.queue_url == "redis://:change_me@127.0.0.1:6379/0"


def test_settings_keep_existing_queue_url_credentials(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_URL", "redis://:already_set@127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "change_me")

    settings = NewsFetcherSettings.from_env()

    assert settings.queue_url == "redis://:already_set@127.0.0.1:6379/0"


def test_settings_include_retry_drain_batch_size(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_PUBLISH_RETRY_DRAIN_BATCH_SIZE", "250")

    settings = NewsFetcherSettings.from_env()

    assert settings.publish_retry_drain_batch_size == 250


def test_settings_include_news_fetcher_log_file(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_FETCHER_LOG_FILE", "logs/custom-news-fetcher.log")

    settings = NewsFetcherSettings.from_env()

    assert settings.log_file == "logs/custom-news-fetcher.log"


def test_settings_prefer_news_fetcher_log_level_over_shared_log_level(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("NEWS_FETCHER_LOG_LEVEL", "DEBUG")

    settings = NewsFetcherSettings.from_env()

    assert settings.log_level == "DEBUG"
