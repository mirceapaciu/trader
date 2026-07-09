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


def test_settings_company_news_defaults(monkeypatch) -> None:
    monkeypatch.delenv("NEWS_SOURCE_FINNHUB_COMPANY_NEWS_ENABLED", raising=False)
    monkeypatch.delenv("FINNHUB_COMPANY_NEWS_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("FINNHUB_COMPANY_NEWS_EXCHANGES", raising=False)

    settings = NewsFetcherSettings.from_env()

    assert settings.finnhub_company_news_enabled is True
    assert settings.finnhub_company_news_min_interval_seconds == 120
    assert settings.finnhub_company_news_exchange_codes == ("XNAS", "XNYS")


def test_settings_company_news_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_SOURCE_FINNHUB_COMPANY_NEWS_ENABLED", "false")
    monkeypatch.setenv("FINNHUB_COMPANY_NEWS_MIN_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("FINNHUB_COMPANY_NEWS_EXCHANGES", "xnas, xnys, arcx")
    monkeypatch.setenv("FINNHUB_API_KEY", " key ")

    settings = NewsFetcherSettings.from_env()

    assert settings.finnhub_company_news_enabled is False
    assert settings.finnhub_company_news_min_interval_seconds == 300
    assert settings.finnhub_company_news_exchange_codes == ("XNAS", "XNYS", "ARCX")
    assert settings.finnhub_api_key == "key"
