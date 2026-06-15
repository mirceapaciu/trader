from __future__ import annotations

from datetime import datetime, timezone

from src.product_components.market_data.context import build_market_context
from src.product_components.market_data.models import ContextSourceStatus, FetchRun, MarketContextSnapshot, MarketDataProvider, ProviderSymbol
from src.product_components.market_data.providers import MarketDataProviderClient
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter


class MarketDataService:
    """Coordinates provider fetches and cache updates for watched instruments."""

    def __init__(
        self,
        *,
        storage: PostgresMarketDataStorageAdapter,
        provider_clients: dict[MarketDataProvider, MarketDataProviderClient],
        quote_max_age_seconds: int,
        daily_bar_lookback_days: int,
    ) -> None:
        self._storage = storage
        self._provider_clients = provider_clients
        self._quote_max_age_seconds = quote_max_age_seconds
        self._daily_bar_lookback_days = daily_bar_lookback_days

    def get_market_context(
        self,
        *,
        ticker: str,
        exchange_code: str,
        refresh_if_stale: bool = True,
    ) -> MarketContextSnapshot | None:
        snapshot = self._storage.get_market_context(ticker=ticker, exchange_code=exchange_code)
        if refresh_if_stale and (
            snapshot is None
            or snapshot.source_status in {ContextSourceStatus.STALE, ContextSourceStatus.MISSING}
        ):
            self.refresh_instrument(ticker=ticker, exchange_code=exchange_code)
            snapshot = self._storage.get_market_context(ticker=ticker, exchange_code=exchange_code)
        return snapshot

    def refresh_watchlist_once(self) -> None:
        for instrument in self._storage.load_active_instruments():
            self.refresh_instrument(
                ticker=instrument.ticker,
                exchange_code=instrument.exchange_code,
            )

    def refresh_instrument(self, *, ticker: str, exchange_code: str) -> None:
        mappings = self._storage.load_provider_symbols(ticker=ticker, exchange_code=exchange_code)
        for mapping in mappings:
            self._refresh_mapping(mapping)

        quote = self._storage.load_latest_quote(ticker=ticker, exchange_code=exchange_code)
        bars = self._storage.load_bars(
            ticker=ticker,
            exchange_code=exchange_code,
            bar_interval="1d",
            limit=max(50, self._daily_bar_lookback_days),
        )
        snapshot = build_market_context(
            ticker=ticker,
            exchange_code=exchange_code,
            quote=quote,
            bars=bars,
            now=datetime.now(timezone.utc),
            quote_max_age_seconds=self._quote_max_age_seconds,
        )
        self._storage.upsert_context_snapshot(snapshot)

    def _refresh_mapping(self, mapping: ProviderSymbol) -> None:
        client = self._provider_clients.get(mapping.provider)
        if client is None:
            return
        if mapping.provider is MarketDataProvider.IBKR:
            self._fetch_quote(mapping, client)
            self._fetch_daily_bars(mapping, client)
            return
        if mapping.provider is MarketDataProvider.ALPHA_VANTAGE:
            self._fetch_daily_bars(mapping, client)

    def _fetch_quote(self, mapping: ProviderSymbol, client: MarketDataProviderClient) -> None:
        started_at = datetime.now(timezone.utc)
        fetched_count = 0
        status = "success"
        error_code = None
        try:
            quote = client.fetch_quote(mapping)
            if quote is not None:
                self._storage.upsert_quote(quote)
                fetched_count = 1
            self._storage.record_api_usage(
                provider=mapping.provider,
                endpoint="quote",
                called_at=started_at,
            )
        except Exception as exc:  # pragma: no cover - exercised through service tests with broad failure behavior.
            status = "failed"
            error_code = exc.__class__.__name__
        self._storage.record_fetch_run(
            FetchRun(
                provider=mapping.provider,
                operation="quote",
                ticker=mapping.ticker,
                exchange_code=mapping.exchange_code,
                status=status,
                error_code=error_code,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                fetched_count=fetched_count,
            )
        )

    def _fetch_daily_bars(self, mapping: ProviderSymbol, client: MarketDataProviderClient) -> None:
        started_at = datetime.now(timezone.utc)
        fetched_count = 0
        status = "success"
        error_code = None
        try:
            bars = client.fetch_daily_bars(mapping)
            self._storage.upsert_bars(bars)
            fetched_count = len(bars)
            self._storage.record_api_usage(
                provider=mapping.provider,
                endpoint="daily_bars",
                called_at=started_at,
            )
        except Exception as exc:  # pragma: no cover - exercised through service tests with broad failure behavior.
            status = "failed"
            error_code = exc.__class__.__name__
        self._storage.record_fetch_run(
            FetchRun(
                provider=mapping.provider,
                operation="daily_bars",
                ticker=mapping.ticker,
                exchange_code=mapping.exchange_code,
                status=status,
                error_code=error_code,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                fetched_count=fetched_count,
            )
        )
