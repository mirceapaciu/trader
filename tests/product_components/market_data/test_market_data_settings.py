from __future__ import annotations

from src.product_components.market_data.settings import MarketDataSettings


def test_market_data_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_DB_SCHEMA", raising=False)
    monkeypatch.delenv("MARKET_DATA_ALLOW_DELAYED", raising=False)
    monkeypatch.delenv("IBKR_MARKET_DATA_CLIENT_ID", raising=False)

    settings = MarketDataSettings.from_env()

    assert settings.market_data_db_schema == "market_data"
    assert settings.primary_provider == "ibkr"
    assert settings.backfill_provider == "alpha_vantage"
    assert settings.allow_delayed is True
    assert settings.ibkr_market_data_client_id == 2


def test_market_data_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_DATA_QUOTE_MAX_AGE_SECONDS", "600")
    monkeypatch.setenv("MARKET_DATA_ALLOW_DELAYED", "false")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test-key")

    settings = MarketDataSettings.from_env()

    assert settings.quote_max_age_seconds == 600
    assert settings.allow_delayed is False
    assert settings.alpha_vantage_api_key == "test-key"
