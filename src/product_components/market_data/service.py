from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from src.product_components.market_data.context import build_market_context
from src.product_components.market_data.fundamentals_provider import FinnhubFundamentalsClient
from src.product_components.market_data.models import (
    ContextSourceStatus,
    FetchRun,
    Instrument,
    InstrumentFundamentals,
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
        prefer_ibkr_historical: bool = True,
        max_requests_per_minute: int = 5,
        context_max_age_seconds: int = 1800,
        fundamentals_client: FinnhubFundamentalsClient | None = None,
        fundamentals_refresh_hours: int = 24,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage = storage
        self._provider_clients = provider_clients
        self._quote_max_age_seconds = quote_max_age_seconds
        self._daily_bar_lookback_days = daily_bar_lookback_days
        self._context_max_age_seconds = context_max_age_seconds
        self._historical_bars_provider = historical_bars_provider
        self._prefer_ibkr_historical = prefer_ibkr_historical
        self._max_requests_per_minute = max_requests_per_minute
        self._fundamentals_client = fundamentals_client
        self._fundamentals_refresh_hours = fundamentals_refresh_hours
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
        if refresh_if_stale and self._needs_refresh(snapshot):
            self.refresh_instrument(ticker=ticker, exchange_code=exchange_code)
            snapshot = self._storage.get_market_context(ticker=ticker, exchange_code=exchange_code)
        return snapshot

    def _needs_refresh(self, snapshot: MarketContextSnapshot | None) -> bool:
        if snapshot is None:
            return True
        if snapshot.source_status not in {ContextSourceStatus.STALE, ContextSourceStatus.MISSING}:
            return False
        # Every refresh attempt upserts a snapshot with as_of=now, so a STALE snapshot with
        # a recent as_of means providers just failed — cool down instead of retrying on
        # every caller request (the ThesisBuilder consumer loop is single-threaded).
        age = datetime.now(timezone.utc) - snapshot.as_of
        return age > timedelta(seconds=self._context_max_age_seconds)

    def get_fundamentals(
        self,
        *,
        ticker: str,
        exchange_code: str,
        refresh_if_stale: bool = True,
    ) -> InstrumentFundamentals | None:
        """Return company fundamentals, refreshing from Finnhub when stale.

        Any fetch failure falls back to the cached row (or None); fundamentals
        must never block a caller's pipeline.
        """
        snapshot = self._storage.load_latest_fundamentals(
            ticker=ticker, exchange_code=exchange_code
        )
        if (
            refresh_if_stale
            and self._fundamentals_client is not None
            and self._fundamentals_needs_refresh(snapshot)
        ):
            refreshed = self._refresh_fundamentals(ticker=ticker, exchange_code=exchange_code)
            if refreshed:
                snapshot = self._storage.load_latest_fundamentals(
                    ticker=ticker, exchange_code=exchange_code
                )
        return snapshot

    def get_fundamentals_as_of(
        self,
        *,
        ticker: str,
        exchange_code: str,
        as_of: datetime,
    ) -> InstrumentFundamentals | None:
        """Pure cache read of the fundamentals visible at ``as_of`` (regeneration path)."""
        return self._storage.load_fundamentals_as_of(
            ticker=ticker, exchange_code=exchange_code, as_of=as_of
        )

    def _fundamentals_needs_refresh(self, snapshot: InstrumentFundamentals | None) -> bool:
        if snapshot is None:
            return True
        age = datetime.now(timezone.utc) - snapshot.last_checked_at
        return age > timedelta(hours=self._fundamentals_refresh_hours)

    def _refresh_fundamentals(self, *, ticker: str, exchange_code: str) -> bool:
        started_at = datetime.now(timezone.utc)
        status = "success"
        error_code = None
        fetched_count = 0
        try:
            result = self._fundamentals_client.fetch(ticker=ticker)
            now = datetime.now(timezone.utc)
            self._storage.save_fundamentals(
                InstrumentFundamentals(
                    ticker=ticker.strip().upper(),
                    exchange_code=exchange_code.strip().upper(),
                    market_cap_usd=result.market_cap_usd,
                    shares_outstanding=result.shares_outstanding,
                    revenue_ttm_usd=result.revenue_ttm_usd,
                    next_earnings_date=result.next_earnings_date,
                    provider=MarketDataProvider.FINNHUB,
                    fetched_at=now,
                    last_checked_at=now,
                    payload=result.payload,
                )
            )
            fetched_count = 1
            for endpoint in self._fundamentals_client.endpoints:
                self._storage.record_api_usage(
                    provider=MarketDataProvider.FINNHUB,
                    endpoint=endpoint,
                    called_at=started_at,
                )
        except Exception as exc:
            status = "failed"
            error_code = exc.__class__.__name__
        self._storage.record_fetch_run(
            FetchRun(
                provider=MarketDataProvider.FINNHUB,
                operation="fundamentals",
                ticker=ticker,
                exchange_code=exchange_code,
                status=status,
                error_code=error_code,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                fetched_count=fetched_count,
            )
        )
        return fetched_count > 0

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
    ) -> dict[tuple[str, str], str]:
        """Warm the DB with bars for many instruments, rate-limited for the free tier.

        Instruments already covered by the coverage ledger are skipped without consuming the
        request budget, so re-running the same window does no network work. Each actual
        provider request is throttled to ``max_requests_per_minute``.
        """
        unique = _unique_instruments(instruments)
        outcomes: dict[tuple[str, str], str] = {}
        total = len(unique)
        limiter = _RateLimiter(
            self._max_requests_per_minute, clock=self._clock, sleep=self._sleep
        )
        for index, (ticker, exchange_code) in enumerate(unique, start=1):
            mapping = self._resolve_bars_mapping(ticker=ticker, exchange_code=exchange_code)
            if mapping is None or self._provider_clients.get(mapping.provider) is None:
                self._emit_progress(progress, index, total, ticker, "skipped")
                outcomes[(ticker, exchange_code)] = "unavailable"
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
                outcomes[(ticker, exchange_code)] = "cached" if stored else "empty"
                continue
            limiter.acquire()
            fetched_count = self._fetch_historical_bars(
                mapping,
                self._provider_clients[mapping.provider],
                interval=interval,
                start=start,
                end=end,
            )
            status = "fetched" if fetched_count is not None and fetched_count > 0 else "unavailable"
            outcomes[(ticker, exchange_code)] = status
            self._emit_progress(progress, index, total, ticker, status)
        return outcomes

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

    def _ibkr_available(self) -> bool:
        client = self._provider_clients.get(MarketDataProvider.IBKR)
        if client is None:
            return False
        is_available = getattr(client, "is_available", None)
        return bool(is_available()) if callable(is_available) else False

    def _provider_preference(self, exchange_code: str) -> list[MarketDataProvider]:
        canonical = normalize_exchange_code(exchange_code)
        if canonical in _US_EXCHANGES:
            try:
                primary = MarketDataProvider(self._historical_bars_provider)
            except ValueError:
                primary = MarketDataProvider.POLYGON
            # Prefer IBKR first when a live session is available; otherwise fall back to the
            # configured historical provider (Polygon) so a disconnected IBKR never wins.
            if self._prefer_ibkr_historical and self._ibkr_available():
                order = [MarketDataProvider.IBKR, primary, MarketDataProvider.ALPHA_VANTAGE]
            else:
                order = [primary, MarketDataProvider.IBKR, MarketDataProvider.ALPHA_VANTAGE]
        else:
            # Polygon's free tier is US-only; non-US history comes from IBKR.
            order = [MarketDataProvider.IBKR]
        seen: set[MarketDataProvider] = set()
        return [p for p in order if not (p in seen or seen.add(p))]

    def _quote_provider_preference(self, exchange_code: str) -> list[MarketDataProvider]:
        canonical = normalize_exchange_code(exchange_code)
        if canonical in _US_EXCHANGES:
            # Live IBKR session first; skip a known-dead IBKR entirely rather than burn its
            # snapshot timeout. Polygon serves a previous-close fallback (free tier); Alpha
            # Vantage has no quote endpoint at all.
            if self._ibkr_available():
                return [MarketDataProvider.IBKR, MarketDataProvider.POLYGON]
            return [MarketDataProvider.POLYGON]
        return [MarketDataProvider.IBKR]

    def _mapping_for_provider(
        self,
        *,
        ticker: str,
        exchange_code: str,
        provider: MarketDataProvider,
        existing: dict[MarketDataProvider, ProviderSymbol],
    ) -> ProviderSymbol | None:
        if provider in existing:
            return existing[provider]
        try:
            symbol = default_provider_symbol(
                ticker=ticker, exchange_code=exchange_code, provider=provider
            )
        except ValueError:
            return None
        # Persist the freshly discovered mapping so future lookups skip resolution.
        self._storage.upsert_provider_symbol(symbol)
        return symbol

    def _load_provider_symbol_map(
        self, *, ticker: str, exchange_code: str
    ) -> dict[MarketDataProvider, ProviderSymbol]:
        return {
            mapping.provider: mapping
            for mapping in self._storage.load_provider_symbols(
                ticker=ticker, exchange_code=exchange_code
            )
        }

    def _resolve_bars_mapping(self, *, ticker: str, exchange_code: str) -> ProviderSymbol | None:
        existing = self._load_provider_symbol_map(ticker=ticker, exchange_code=exchange_code)
        for provider in self._provider_preference(exchange_code):
            if provider not in self._provider_clients:
                continue
            mapping = self._mapping_for_provider(
                ticker=ticker, exchange_code=exchange_code, provider=provider, existing=existing
            )
            if mapping is not None:
                return mapping
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
    ) -> int | None:
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
        return fetched_count if status == "success" else None

    def refresh_watchlist_once(self) -> None:
        for instrument in self._storage.load_active_instruments():
            self.refresh_instrument(
                ticker=instrument.ticker,
                exchange_code=instrument.exchange_code,
            )

    def refresh_instrument(self, *, ticker: str, exchange_code: str) -> None:
        """Refresh via the retrieval chain: DB cache -> IBKR -> Polygon fallback.

        Quotes walk `_quote_provider_preference` and stop at the first provider that stores
        one; daily bars are refreshed through the existing bars routing only when the cache
        is no longer current. The rebuilt context snapshot is upserted even when every
        provider failed, so `_needs_refresh` can rate-limit retry attempts.
        """
        self._refresh_quote(ticker=ticker, exchange_code=exchange_code)
        self._refresh_daily_bars_if_stale(ticker=ticker, exchange_code=exchange_code)

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

    def _refresh_quote(self, *, ticker: str, exchange_code: str) -> None:
        existing = self._load_provider_symbol_map(ticker=ticker, exchange_code=exchange_code)
        for provider in self._quote_provider_preference(exchange_code):
            client = self._provider_clients.get(provider)
            if client is None:
                continue
            mapping = self._mapping_for_provider(
                ticker=ticker, exchange_code=exchange_code, provider=provider, existing=existing
            )
            if mapping is None:
                continue
            if self._fetch_quote(mapping, client):
                return

    def _refresh_daily_bars_if_stale(self, *, ticker: str, exchange_code: str) -> None:
        bars = self._storage.load_bars(
            ticker=ticker, exchange_code=exchange_code, bar_interval="1d", limit=1
        )
        if bars:
            latest = max(bar.bar_start_at for bar in bars)
            # A bar within ~3 calendar days covers weekends/holidays between sessions;
            # skipping current bars spares the free-tier Polygon request budget.
            if datetime.now(timezone.utc) - latest <= timedelta(days=3):
                return
        mapping = self._resolve_bars_mapping(ticker=ticker, exchange_code=exchange_code)
        if mapping is None:
            return
        client = self._provider_clients.get(mapping.provider)
        if client is None:
            return
        self._fetch_daily_bars(mapping, client)

    def _fetch_quote(self, mapping: ProviderSymbol, client: MarketDataProviderClient) -> bool:
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
        return fetched_count > 0

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
