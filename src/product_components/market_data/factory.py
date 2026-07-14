"""Composition-root factory for MarketDataService.

Every component process (ThesisBuilder, TradeExecutor, backtester CLI, monitoring-UI
backtest runner) needs its own MarketDataService instance wired to the shared Postgres
cache. This factory keeps that wiring in one place; callers choose only whether the
instance can reach live providers (IBKR/Polygon) or is a cache-only reader.

The caller owns the returned gateway's lifecycle and must ``disconnect()`` it on
shutdown (it is ``None`` for cache-only instances or when IBKR-first is disabled).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.product_components.market_data.fundamentals_provider import FinnhubFundamentalsClient
from src.product_components.market_data.providers import build_provider_clients
from src.product_components.market_data.service import MarketDataService
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.market_data.storage_adapter import (
    InstrumentRegistry,
    PostgresMarketDataStorageAdapter,
)
from src.product_components.shared.adapters import (
    PostgresSharedApiUsageWriter,
    PostgresSharedInstrumentRegistry,
)

if TYPE_CHECKING:
    from src.product_components.market_data.ibkr_gateway import IbAsyncMarketDataGateway


def build_market_data_service(
    settings: MarketDataSettings,
    *,
    instrument_registry: InstrumentRegistry | None = None,
    with_providers: bool = True,
) -> "tuple[MarketDataService, IbAsyncMarketDataGateway | None]":
    """Build a MarketDataService wired to the shared Postgres cache.

    ``with_providers=True`` attaches the live retrieval chain (best-effort IBKR gateway
    connect, Polygon/Alpha Vantage clients); ``False`` yields a cache-only reader whose
    refreshes are no-ops — for consumers that rely on another process keeping the cache
    warm. Pass ``instrument_registry`` when the caller shares one with other components;
    otherwise a registry is built from the same settings.
    """
    if instrument_registry is None:
        instrument_registry = PostgresSharedInstrumentRegistry(
            dsn=settings.postgres_dsn,
            shared_schema=settings.shared_db_schema,
            watchlist_table=settings.watchlist_table,
        )

    ibkr_gateway = None
    provider_clients = {}
    fundamentals_client = None
    if with_providers:
        # Imported lazily so cache-only consumers never pull in ib_async.
        from src.product_components.market_data.ibkr_gateway import (
            build_market_data_ibkr_gateway,
        )

        ibkr_gateway = build_market_data_ibkr_gateway(settings)
        provider_clients = build_provider_clients(settings, ibkr_gateway=ibkr_gateway)
        if settings.fundamentals_enabled and settings.finnhub_api_key.strip():
            fundamentals_client = FinnhubFundamentalsClient(api_key=settings.finnhub_api_key)

    service = MarketDataService(
        storage=PostgresMarketDataStorageAdapter(
            dsn=settings.postgres_dsn,
            market_data_schema=settings.market_data_db_schema,
            instrument_registry=instrument_registry,
            api_usage_writer=PostgresSharedApiUsageWriter(
                dsn=settings.postgres_dsn,
                shared_schema=settings.shared_db_schema,
            ),
        ),
        provider_clients=provider_clients,
        quote_max_age_seconds=settings.quote_max_age_seconds,
        daily_bar_lookback_days=settings.daily_bar_lookback_days,
        historical_bars_provider=settings.historical_bars_provider,
        prefer_ibkr_historical=settings.prefer_ibkr_historical,
        max_requests_per_minute=settings.polygon_max_requests_per_minute,
        context_max_age_seconds=settings.context_max_age_seconds,
        fundamentals_client=fundamentals_client,
        fundamentals_refresh_hours=settings.fundamentals_refresh_hours,
    )
    return service, ibkr_gateway
