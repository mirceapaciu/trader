from __future__ import annotations

from src.product_components.market_data.factory import build_market_data_service
from src.product_components.market_data.settings import MarketDataSettings


def test_cache_only_factory_builds_service_without_providers_or_gateway() -> None:
    service, gateway = build_market_data_service(
        MarketDataSettings.from_env(), with_providers=False
    )

    assert gateway is None
    assert service._provider_clients == {}


def test_factory_wires_settings_into_the_service() -> None:
    settings = MarketDataSettings.from_env()
    service, _ = build_market_data_service(settings, with_providers=False)

    assert service._quote_max_age_seconds == settings.quote_max_age_seconds
    assert service._context_max_age_seconds == settings.context_max_age_seconds
    assert service._daily_bar_lookback_days == settings.daily_bar_lookback_days
    assert service._historical_bars_provider == settings.historical_bars_provider
