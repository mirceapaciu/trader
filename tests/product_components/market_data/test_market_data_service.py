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
        now = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
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
        now = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
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
