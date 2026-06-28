from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from src.product_components.market_data.context import build_market_context
from src.product_components.market_data.models import (
    ContextSourceStatus,
    FetchRun,
    Instrument,
    MarketBar,
    MarketContextSnapshot,
    MarketDataProvider,
    ProviderSymbol,
)
from src.product_components.market_data.provider_symbols import (
    default_provider_symbol,
    normalize_exchange_code,
)
from src.product_components.market_data.providers import MarketDataProviderClient
from src.product_components.market_data.storage_adapter import PostgresMarketDataStorageAdapter

# US exchanges the free Polygon tier covers; everything else routes to IBKR.
_US_EXCHANGES = {"XNAS", "XNYS"}

# Progress callback: (done, total, ticker, status) where status is one of
# "fetched" | "cached" | "skipped".
PrefetchProgress = Callable[[int, int, str, str], None]


class MarketDataService:
    """Coordinates provider fetches and cache updates for watched instruments."""

    def __init__(
        self,
        *,
        storage: PostgresMarketDataStorageAdapter,
        provider_clients: dict[MarketDataProvider, MarketDataProviderClient],
        quote_max_age_seconds: int,
        daily_bar_lookback_days: int,
        historical_bars_provider: str = "polygon",
        max_requests_per_minute: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage = storage
        self._provider_clients = provider_clients
        self._quote_max_age_seconds = quote_max_age_seconds
        self._daily_bar_lookback_days = daily_bar_lookback_days
        self._historical_bars_provider = historical_bars_provider
        self._max_requests_per_minute = max_requests_per_minute
        self._clock = clock
        self._sleep = sleep

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

    def get_historical_bars(
        self,
        *,
        ticker: str,
        exchange_code: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[MarketBar]:
        stored = self._storage.load_bars_in_range(
            ticker=ticker,
            exchange_code=exchange_code,
            bar_interval=interval,
            start=start,
            end=end,
            adjusted=False,
        )
        mapping = self._resolve_bars_mapping(ticker=ticker, exchange_code=exchange_code)
        if mapping is not None and not self._is_covered(
            stored, mapping=mapping, interval=interval, start=start, end=end
        ):
            client = self._provider_clients.get(mapping.provider)
            if client is not None:
                self._fetch_historical_bars(
                    mapping,
                    client,
                    interval=interval,
                    start=start,
                    end=end,
                )
                stored = self._storage.load_bars_in_range(
                    ticker=ticker,
                    exchange_code=exchange_code,
                    bar_interval=interval,
                    start=start,
                    end=end,
                    adjusted=False,
                )
        return sorted(
            (bar for bar in stored if start <= bar.bar_start_at <= end),
            key=lambda bar: bar.bar_start_at,
        )

    def prefetch_historical_bars(
        self,
        instruments: Iterable[Instrument | tuple[str, str]],
        *,
        interval: str,
        start: datetime,
        end: datetime,
        progress: PrefetchProgress | None = None,
    ) -> None:
        """Warm the DB with bars for many instruments, rate-limited for the free tier.

        Instruments already covered by the coverage ledger are skipped without consuming the
        request budget, so re-running the same window does no network work. Each actual
        provider request is throttled to ``max_requests_per_minute``.
        """
        unique = _unique_instruments(instruments)
        total = len(unique)
        limiter = _RateLimiter(
            self._max_requests_per_minute, clock=self._clock, sleep=self._sleep
        )
        for index, (ticker, exchange_code) in enumerate(unique, start=1):
            mapping = self._resolve_bars_mapping(ticker=ticker, exchange_code=exchange_code)
            if mapping is None or self._provider_clients.get(mapping.provider) is None:
                self._emit_progress(progress, index, total, ticker, "skipped")
                continue
            stored = self._storage.load_bars_in_range(
                ticker=ticker,
                exchange_code=exchange_code,
                bar_interval=interval,
                start=start,
                end=end,
                adjusted=False,
            )
            if self._is_covered(stored, mapping=mapping, interval=interval, start=start, end=end):
                self._emit_progress(progress, index, total, ticker, "cached")
                continue
            limiter.acquire()
            self._fetch_historical_bars(
                mapping,
                self._provider_clients[mapping.provider],
                interval=interval,
                start=start,
                end=end,
            )
            self._emit_progress(progress, index, total, ticker, "fetched")

    @staticmethod
    def _emit_progress(
        progress: PrefetchProgress | None,
        done: int,
        total: int,
        ticker: str,
        status: str,
    ) -> None:
        if progress is not None:
            progress(done, total, ticker, status)

    def _provider_preference(self, exchange_code: str) -> list[MarketDataProvider]:
        canonical = normalize_exchange_code(exchange_code)
        if canonical in _US_EXCHANGES:
            try:
                primary = MarketDataProvider(self._historical_bars_provider)
            except ValueError:
                primary = MarketDataProvider.POLYGON
            order = [primary, MarketDataProvider.IBKR, MarketDataProvider.ALPHA_VANTAGE]
        else:
            # Polygon's free tier is US-only; non-US history comes from IBKR.
            order = [MarketDataProvider.IBKR]
        seen: set[MarketDataProvider] = set()
        return [p for p in order if not (p in seen or seen.add(p))]

    def _resolve_bars_mapping(self, *, ticker: str, exchange_code: str) -> ProviderSymbol | None:
        existing = {
            mapping.provider: mapping
            for mapping in self._storage.load_provider_symbols(
                ticker=ticker, exchange_code=exchange_code
            )
        }
        for provider in self._provider_preference(exchange_code):
            if provider not in self._provider_clients:
                continue
            if provider in existing:
                return existing[provider]
            try:
                symbol = default_provider_symbol(
                    ticker=ticker, exchange_code=exchange_code, provider=provider
                )
            except ValueError:
                continue
            # Persist the freshly discovered mapping so future lookups skip resolution.
            self._storage.upsert_provider_symbol(symbol)
            return symbol
        for provider, symbol in existing.items():
            if provider in self._provider_clients:
                return symbol
        return None

    def _is_covered(
        self,
        bars: list[MarketBar],
        *,
        mapping: ProviderSymbol,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        if bars:
            earliest = min(bar.bar_start_at for bar in bars)
            latest = max(bar.bar_start_at for bar in bars)
            if earliest <= start and latest >= end:
                return True
        coverage = self._storage.load_bar_coverage(
            ticker=mapping.ticker,
            exchange_code=mapping.exchange_code,
            provider=mapping.provider,
            bar_interval=interval,
        )
        return coverage is not None and coverage[0] <= start and coverage[1] >= end

    def _fetch_historical_bars(
        self,
        mapping: ProviderSymbol,
        client: MarketDataProviderClient,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        fetched_count = 0
        status = "success"
        error_code = None
        try:
            bars = client.fetch_historical_bars(
                mapping,
                interval=interval,
                start=start,
                end=end,
            )
            self._storage.upsert_bars(bars)
            fetched_count = len(bars)
            self._storage.record_api_usage(
                provider=mapping.provider,
                endpoint="historical_bars",
                called_at=started_at,
            )
            # Record the requested window as covered even when sparse (weekends/holidays),
            # so subsequent reads of the same range do not refetch.
            self._storage.upsert_bar_coverage(
                ticker=mapping.ticker,
                exchange_code=mapping.exchange_code,
                provider=mapping.provider,
                bar_interval=interval,
                covered_start=start,
                covered_end=end,
            )
        except Exception as exc:  # pragma: no cover - exercised through service tests with broad failure behavior.
            status = "failed"
            error_code = exc.__class__.__name__
        self._storage.record_fetch_run(
            FetchRun(
                provider=mapping.provider,
                operation="historical_bars",
                ticker=mapping.ticker,
                exchange_code=mapping.exchange_code,
                status=status,
                error_code=error_code,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                fetched_count=fetched_count,
            )
        )

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


class _RateLimiter:
    """Token-free min-interval throttle: spaces calls by ``60 / requests_per_minute`` seconds."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None

    def acquire(self) -> None:
        if self._min_interval <= 0.0:
            return
        now = self._clock()
        if self._last_call is not None:
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_call = now


def _unique_instruments(
    instruments: Iterable[Instrument | tuple[str, str]],
) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for item in instruments:
        if isinstance(item, Instrument):
            key = (item.ticker.strip().upper(), item.exchange_code.strip().upper())
        else:
            key = (item[0].strip().upper(), item[1].strip().upper())
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result
