from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.product_components.market_data.models import (
    ContextSourceStatus,
    FetchRun,
    Instrument,
    MarketBar,
    MarketContextSnapshot,
    MarketDataProvider,
    MarketQuote,
    ProviderSymbol,
    QuoteDataType,
)
from src.product_components.market_data.service import MarketDataService


class _FakeClient:
    provider = MarketDataProvider.IBKR

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote:
        now = datetime.now(timezone.utc)
        return MarketQuote(
            ticker=symbol.ticker,
            exchange_code=symbol.exchange_code,
            provider=symbol.provider,
            data_type=QuoteDataType.DELAYED,
            currency=symbol.currency,
            bid_price=99,
            ask_price=101,
            last_price=100,
            previous_close=98,
            volume=1000,
            provider_timestamp=now,
            fetched_at=now,
        )

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        now = datetime.now(timezone.utc)
        return [
            MarketBar(
                ticker=symbol.ticker,
                exchange_code=symbol.exchange_code,
                provider=symbol.provider,
                bar_interval="1d",
                bar_start_at=now - timedelta(days=index),
                currency=symbol.currency,
                open_price=90 + index,
                high_price=91 + index,
                low_price=89 + index,
                close_price=90 + index,
                volume=1000,
                adjusted=False,
                fetched_at=now,
            )
            for index in range(60)
        ]


class _FakeStorage:
    def __init__(self) -> None:
        self.instrument = Instrument(ticker="RHM", exchange_code="XETR")
        self.mapping = ProviderSymbol(
            ticker="RHM",
            exchange_code="XETR",
            provider=MarketDataProvider.IBKR,
            provider_symbol="RHM",
            currency="EUR",
        )
        self.quote: MarketQuote | None = None
        self.bars: list[MarketBar] = []
        self.fetch_runs: list[FetchRun] = []
        self.api_usage: list[tuple[MarketDataProvider, str]] = []
        self.context_count = 0
        self.context: MarketContextSnapshot | None = None

    def load_active_instruments(self) -> list[Instrument]:
        return [self.instrument]

    def load_provider_symbols(self, *, ticker: str, exchange_code: str):
        return [self.mapping]

    def upsert_quote(self, quote: MarketQuote) -> None:
        self.quote = quote

    def upsert_bars(self, bars: list[MarketBar]) -> None:
        self.bars = bars

    def load_latest_quote(self, *, ticker: str, exchange_code: str):
        return self.quote

    def load_bars(self, *, ticker: str, exchange_code: str, bar_interval: str, limit: int):
        return self.bars

    def upsert_context_snapshot(self, snapshot) -> None:
        self.context_count += 1
        self.context = snapshot

    def get_market_context(self, *, ticker: str, exchange_code: str):
        return self.context

    def record_fetch_run(self, run: FetchRun) -> None:
        self.fetch_runs.append(run)

    def record_api_usage(self, *, provider: MarketDataProvider, endpoint: str, called_at: datetime) -> None:
        self.api_usage.append((provider, endpoint))


def test_market_data_service_refreshes_mapping_and_context() -> None:
    storage = _FakeStorage()
    service = MarketDataService(
        storage=storage,  # type: ignore[arg-type]
        provider_clients={MarketDataProvider.IBKR: _FakeClient()},
        quote_max_age_seconds=7200,
        daily_bar_lookback_days=90,
    )

    service.refresh_watchlist_once()

    assert storage.quote is not None
    assert len(storage.bars) == 60
    assert [run.operation for run in storage.fetch_runs] == ["quote", "daily_bars"]
    assert storage.api_usage == [
        (MarketDataProvider.IBKR, "quote"),
        (MarketDataProvider.IBKR, "daily_bars"),
    ]
    assert storage.context_count == 1


def test_market_data_service_refreshes_stale_context_on_api_request() -> None:
    storage = _FakeStorage()
    storage.context = MarketContextSnapshot(
        ticker="RHM",
        exchange_code="XETR",
        as_of=datetime(2026, 6, 15, 11, tzinfo=timezone.utc),
        source_status=ContextSourceStatus.STALE,
        current_price=None,
        previous_close=None,
        return_1d=None,
        return_5d=None,
        return_20d=None,
        atr_20d=None,
        volatility_20d=None,
        volume_ratio_20d=None,
        sma_20d=None,
        sma_50d=None,
        recent_high_20d=None,
        recent_low_20d=None,
        drawdown_from_high_20d=None,
        quote_fetched_at=None,
        bars_fetched_at=None,
    )
    service = MarketDataService(
        storage=storage,  # type: ignore[arg-type]
        provider_clients={MarketDataProvider.IBKR: _FakeClient()},
        quote_max_age_seconds=7200,
        daily_bar_lookback_days=90,
    )

    context = service.get_market_context(ticker="RHM", exchange_code="XETR", refresh_if_stale=True)

    assert context is not None
    assert context.source_status is ContextSourceStatus.DELAYED
    assert storage.context_count == 1


class _ChainClient:
    """Configurable fake: serves a quote (or None) and optional daily bars."""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        quote: bool,
        available: bool = True,
        bars: bool = True,
    ) -> None:
        self.provider = provider
        self._quote = quote
        self._available = available
        self._bars = bars
        self.quote_calls = 0
        self.bar_calls = 0

    def is_available(self) -> bool:
        return self._available

    def fetch_quote(self, symbol: ProviderSymbol) -> MarketQuote | None:
        self.quote_calls += 1
        if not self._quote:
            return None
        now = datetime.now(timezone.utc)
        return MarketQuote(
            ticker=symbol.ticker,
            exchange_code=symbol.exchange_code,
            provider=self.provider,
            data_type=QuoteDataType.DELAYED,
            currency=symbol.currency,
            bid_price=99,
            ask_price=101,
            last_price=100,
            previous_close=98,
            volume=1000,
            provider_timestamp=now,
            fetched_at=now,
        )

    def fetch_daily_bars(self, symbol: ProviderSymbol, *, outputsize: str = "compact") -> list[MarketBar]:
        self.bar_calls += 1
        if not self._bars:
            return []
        now = datetime.now(timezone.utc)
        return [
            MarketBar(
                ticker=symbol.ticker,
                exchange_code=symbol.exchange_code,
                provider=self.provider,
                bar_interval="1d",
                bar_start_at=now - timedelta(days=index),
                currency=symbol.currency,
                open_price=90 + index,
                high_price=91 + index,
                low_price=89 + index,
                close_price=90 + index,
                volume=1000,
                adjusted=False,
                fetched_at=now,
            )
            for index in range(60)
        ]

    def fetch_historical_bars(self, symbol, *, interval, start, end):
        return []


class _ChainStorage:
    """In-memory storage without pre-seeded provider symbols (they get synthesized)."""

    def __init__(self) -> None:
        self.provider_symbols: list[ProviderSymbol] = []
        self.quote: MarketQuote | None = None
        self.bars: list[MarketBar] = []
        self.fetch_runs: list[FetchRun] = []
        self.context: MarketContextSnapshot | None = None
        self.context_count = 0

    def load_provider_symbols(self, *, ticker: str, exchange_code: str, provider=None):
        return [
            symbol
            for symbol in self.provider_symbols
            if symbol.ticker == ticker.upper() and symbol.exchange_code == exchange_code.upper()
        ]

    def upsert_provider_symbol(self, symbol: ProviderSymbol) -> None:
        self.provider_symbols.append(symbol)

    def upsert_quote(self, quote: MarketQuote) -> None:
        self.quote = quote

    def load_latest_quote(self, *, ticker: str, exchange_code: str):
        return self.quote

    def load_bars(self, *, ticker: str, exchange_code: str, bar_interval: str, limit: int):
        ordered = sorted(self.bars, key=lambda bar: bar.bar_start_at, reverse=True)
        return sorted(ordered[:limit], key=lambda bar: bar.bar_start_at)

    def upsert_bars(self, bars: list[MarketBar]) -> None:
        self.bars = bars

    def upsert_context_snapshot(self, snapshot) -> None:
        self.context_count += 1
        self.context = snapshot

    def get_market_context(self, *, ticker: str, exchange_code: str):
        return self.context

    def record_fetch_run(self, run: FetchRun) -> None:
        self.fetch_runs.append(run)

    def record_api_usage(self, *, provider, endpoint, called_at) -> None:
        pass


def _chain_service(storage, clients, **kwargs) -> MarketDataService:
    return MarketDataService(
        storage=storage,  # type: ignore[arg-type]
        provider_clients=clients,
        quote_max_age_seconds=7200,
        daily_bar_lookback_days=90,
        **kwargs,
    )


def _stale_snapshot(as_of: datetime, *, status=ContextSourceStatus.STALE) -> MarketContextSnapshot:
    return MarketContextSnapshot(
        ticker="AAPL",
        exchange_code="XNAS",
        as_of=as_of,
        source_status=status,
        current_price=None,
        previous_close=None,
        return_1d=None,
        return_5d=None,
        return_20d=None,
        atr_20d=None,
        volatility_20d=None,
        volume_ratio_20d=None,
        sma_20d=None,
        sma_50d=None,
        recent_high_20d=None,
        recent_low_20d=None,
        drawdown_from_high_20d=None,
        quote_fetched_at=None,
        bars_fetched_at=None,
    )


def test_quote_chain_stops_at_ibkr_when_it_delivers() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    polygon = _ChainClient(MarketDataProvider.POLYGON, quote=True)
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr, MarketDataProvider.POLYGON: polygon}
    )

    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    assert ibkr.quote_calls == 1
    assert polygon.quote_calls == 0
    assert storage.quote is not None
    assert storage.quote.provider is MarketDataProvider.IBKR


def test_quote_chain_falls_back_to_polygon_when_ibkr_returns_nothing() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=False)
    polygon = _ChainClient(MarketDataProvider.POLYGON, quote=True)
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr, MarketDataProvider.POLYGON: polygon}
    )

    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    assert ibkr.quote_calls == 1
    assert polygon.quote_calls == 1
    assert storage.quote is not None
    assert storage.quote.provider is MarketDataProvider.POLYGON


def test_quote_chain_skips_dead_ibkr_entirely() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True, available=False)
    polygon = _ChainClient(MarketDataProvider.POLYGON, quote=True)
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr, MarketDataProvider.POLYGON: polygon}
    )

    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    assert ibkr.quote_calls == 0
    assert polygon.quote_calls == 1


def test_refresh_upserts_stale_snapshot_even_when_all_providers_fail() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=False, bars=False)
    polygon = _ChainClient(MarketDataProvider.POLYGON, quote=False, bars=False)
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr, MarketDataProvider.POLYGON: polygon}
    )

    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    quote_runs = [run for run in storage.fetch_runs if run.operation == "quote"]
    assert [run.provider for run in quote_runs] == [
        MarketDataProvider.IBKR,
        MarketDataProvider.POLYGON,
    ]
    # The failed-refresh snapshot is still recorded so the cooldown can rate-limit retries.
    assert storage.context_count == 1
    assert storage.context.source_status is ContextSourceStatus.MISSING


def test_refresh_synthesizes_provider_mappings_when_none_exist() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    service = _chain_service(
        storage,
        {
            MarketDataProvider.IBKR: ibkr,
            MarketDataProvider.POLYGON: _ChainClient(MarketDataProvider.POLYGON, quote=True),
        },
    )

    assert storage.provider_symbols == []
    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    assert any(s.provider is MarketDataProvider.IBKR for s in storage.provider_symbols)
    assert storage.quote is not None
    assert len(storage.bars) == 60


def test_refresh_skips_daily_bars_when_cache_is_current() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    now = datetime.now(timezone.utc)
    storage.bars = [
        MarketBar(
            ticker="AAPL",
            exchange_code="XNAS",
            provider=MarketDataProvider.POLYGON,
            bar_interval="1d",
            bar_start_at=now - timedelta(days=1),
            currency="USD",
            open_price=100,
            high_price=101,
            low_price=99,
            close_price=100,
            volume=1000,
            adjusted=False,
            fetched_at=now,
        )
    ]
    service = _chain_service(storage, {MarketDataProvider.IBKR: ibkr})

    service.refresh_instrument(ticker="AAPL", exchange_code="XNAS")

    assert ibkr.bar_calls == 0
    assert ibkr.quote_calls == 1


def test_get_market_context_cooldown_suppresses_retry_after_recent_failure() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    storage.context = _stale_snapshot(datetime.now(timezone.utc))
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr}, context_max_age_seconds=300
    )

    context = service.get_market_context(ticker="AAPL", exchange_code="XNAS", refresh_if_stale=True)

    assert context is storage.context
    assert ibkr.quote_calls == 0
    assert storage.fetch_runs == []


def test_get_market_context_refreshes_once_cooldown_expires() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    storage.context = _stale_snapshot(datetime.now(timezone.utc) - timedelta(seconds=301))
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr}, context_max_age_seconds=300
    )

    context = service.get_market_context(ticker="AAPL", exchange_code="XNAS", refresh_if_stale=True)

    assert ibkr.quote_calls == 1
    assert context is not None
    assert context.source_status is ContextSourceStatus.DELAYED


def test_get_market_context_missing_snapshot_always_refreshes() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr}, context_max_age_seconds=300
    )

    context = service.get_market_context(ticker="AAPL", exchange_code="XNAS", refresh_if_stale=True)

    assert ibkr.quote_calls == 1
    assert context is not None


def test_get_market_context_fresh_snapshot_is_served_from_cache() -> None:
    storage = _ChainStorage()
    ibkr = _ChainClient(MarketDataProvider.IBKR, quote=True)
    storage.context = _stale_snapshot(
        datetime.now(timezone.utc) - timedelta(days=2), status=ContextSourceStatus.FRESH
    )
    service = _chain_service(
        storage, {MarketDataProvider.IBKR: ibkr}, context_max_age_seconds=300
    )

    context = service.get_market_context(ticker="AAPL", exchange_code="XNAS", refresh_if_stale=True)

    assert context is storage.context
    assert ibkr.quote_calls == 0
