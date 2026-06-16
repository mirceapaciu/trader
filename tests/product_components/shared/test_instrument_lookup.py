from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import psycopg

from src.product_components.shared.adapters import (
    SharedLookupCacheEntry,
    SharedWatchlistRecord,
)
from src.product_components.shared.instrument_lookup import (
    InstrumentLookupSuggestion,
    SharedInstrumentLookupAdminService,
)


class FakeRegistry:
    def __init__(self) -> None:
        self.rows = [
            SharedWatchlistRecord(
                ticker="NVDA",
                exchange_code="XNAS",
                display_name="NVIDIA Corporation",
                aliases=(),
                is_active=True,
                source="manual",
            )
        ]

    def list_watchlist_records(self, *, active_only: bool = True):
        return self.rows

    def get_watchlist_record(self, *, ticker: str, exchange_code: str):
        for row in self.rows:
            if row.ticker == ticker and row.exchange_code == exchange_code:
                return row
        return None


class FakeAdmin:
    def __init__(self) -> None:
        self.cache: dict[tuple[str, str], SharedLookupCacheEntry] = {}
        self.saved_provider: str | None = None
        self.deactivated: tuple[str, str] | None = None

    def load_lookup_cache(self, *, operation: str, target: str):
        return self.cache.get((operation, target))

    def save_lookup_cache(self, *, operation: str, target: str, provider: str, payload: dict, fetched_at, expires_at):
        self.saved_provider = provider
        self.cache[(operation, target)] = SharedLookupCacheEntry(
            operation=operation,
            target=target,
            provider=provider,
            payload=payload,
            fetched_at=fetched_at,
            expires_at=expires_at,
        )

    def upsert_watchlist_entry(self, entry, *, replace_aliases: bool = True):
        return None

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str):
        self.deactivated = (ticker, exchange_code)


class FakeProvider:
    def __init__(self, *, name: str, results: list[InstrumentLookupSuggestion], raises: bool = False) -> None:
        self.provider_name = name
        self._results = results
        self._raises = raises
        self.search_calls = 0
        self.queries: list[str] = []

    def search(self, query: str) -> list[InstrumentLookupSuggestion]:
        self.search_calls += 1
        self.queries.append(query)
        if self._raises:
            raise RuntimeError("provider error")
        return self._results

    def discover_aliases(self, *, ticker: str, exchange_code: str, display_name: str | None):
        if self._raises:
            raise RuntimeError("provider error")
        for result in self._results:
            if result.ticker == ticker and result.exchange_code == exchange_code:
                return result
        return None


def test_lookup_cache_hit_returns_cached_results_without_calling_provider() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.cache[("search", "nvidia")] = SharedLookupCacheEntry(
        operation="search",
        target="nvidia",
        provider="massive",
        payload={
            "results": [
                {
                    "ticker": "NVDA",
                    "exchange_code": "XNAS",
                    "display_name": "NVIDIA Corporation",
                    "aliases": ["nvidia"],
                    "provider": "massive",
                }
            ]
        },
        fetched_at=_now(),
        expires_at=_now() + timedelta(hours=1),
    )
    provider = FakeProvider(
        name="massive",
        results=[],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("nvidia")

    assert cached is True
    assert provider.search_calls == 0
    assert results[0].ticker == "NVDA"


def test_lookup_ignores_cached_empty_results_and_refetches_provider() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.cache[("search", "ford")] = SharedLookupCacheEntry(
        operation="search",
        target="ford",
        provider="massive",
        payload={"results": []},
        fetched_at=_now(),
        expires_at=_now() + timedelta(hours=1),
    )
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford", "ford motor company"),
                provider="massive",
            )
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Ford")

    assert cached is False
    assert provider.search_calls == 1
    assert results[0].ticker == "F"


def test_lookup_falls_back_when_primary_provider_fails() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    primary = FakeProvider(name="massive", results=[], raises=True)
    fallback = FakeProvider(
        name="alpha_vantage",
        results=[
            InstrumentLookupSuggestion(
                ticker="NVDA",
                exchange_code="XNAS",
                display_name="NVIDIA Corporation",
                aliases=("nvidia",),
                provider="alpha_vantage",
            )
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(primary, fallback),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("nvidia")

    assert cached is False
    assert primary.search_calls == 1
    assert fallback.search_calls == 1
    assert admin.saved_provider == "alpha_vantage"
    assert results[0].provider == "alpha_vantage"


def test_watchlist_listing_gracefully_degrades_when_registry_is_unavailable() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    def broken_list_watchlist_records(*, active_only: bool = True):
        raise psycopg.errors.ConnectionTimeout("timeout expired")

    registry.list_watchlist_records = broken_list_watchlist_records  # type: ignore[method-assign]

    assert service.list_watchlist() == []


def test_alias_discovery_uses_cache_after_first_fetch() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="NVDA",
                exchange_code="XNAS",
                display_name="NVIDIA Corporation",
                aliases=("nvidia", "nvidia corporation"),
                provider="massive",
            )
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    first, first_cached = service.discover_aliases(
        ticker="NVDA",
        exchange_code="XNAS",
        display_name="NVIDIA Corporation",
    )
    second, second_cached = service.discover_aliases(
        ticker="NVDA",
        exchange_code="XNAS",
        display_name="NVIDIA Corporation",
    )

    assert first is not None
    assert first_cached is False
    assert second is not None
    assert second_cached is True


def test_lookup_retries_with_normalized_hyphenated_query() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()

    class HyphenAwareProvider(FakeProvider):
        def search(self, query: str) -> list[InstrumentLookupSuggestion]:
            self.search_calls += 1
            self.queries.append(query)
            if query in {"Novo Nordisk", "novo nordisk"}:
                return [
                    InstrumentLookupSuggestion(
                        ticker="NVO",
                        exchange_code="XNYS",
                        display_name="Novo Nordisk A/S",
                        aliases=("novo nordisk",),
                        provider="massive",
                    )
                ]
            return []

    provider = HyphenAwareProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Novo-Nordisk")

    assert cached is False
    assert provider.queries == ["Novo-Nordisk", "novo nordisk"]
    assert results[0].ticker == "NVO"


def test_lookup_coalesces_concurrent_external_calls_for_same_query() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    search_started = threading.Event()
    release_search = threading.Event()
    results_by_thread: list[tuple[list[InstrumentLookupSuggestion], bool]] = []

    class BlockingProvider(FakeProvider):
        def search(self, query: str) -> list[InstrumentLookupSuggestion]:
            self.search_calls += 1
            self.queries.append(query)
            search_started.set()
            release_search.wait(timeout=2)
            return [
                InstrumentLookupSuggestion(
                    ticker="F",
                    exchange_code="XNYS",
                    display_name="Ford Motor Company",
                    aliases=("ford", "ford motor company"),
                    provider="massive",
                )
            ]

    provider = BlockingProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
        lookup_provider_debounce_ms=0,
    )

    def _lookup() -> None:
        results_by_thread.append(service.lookup("Ford"))

    first = threading.Thread(target=_lookup)
    second = threading.Thread(target=_lookup)
    first.start()
    assert search_started.wait(timeout=2)
    second.start()
    release_search.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert provider.search_calls == 1
    assert len(results_by_thread) == 2
    assert results_by_thread[0][0][0].ticker == "F"
    assert results_by_thread[1][0][0].ticker == "F"


def test_lookup_ranks_exact_alias_word_match_above_embedded_substring_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="HIG",
                exchange_code="XNYS",
                display_name="The Hartford Financial Services Group, Inc.",
                aliases=("hartford", "the hartford"),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford", "ford motor company"),
                provider="massive",
            ),
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Ford")

    assert cached is False
    assert [item.ticker for item in results] == ["F", "HIG"]


def test_lookup_ranks_exact_ticker_match_above_display_name_substring_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="TSM",
                exchange_code="XNYS",
                display_name="Taiwan Semiconductor Manufacturing Company Limited",
                aliases=("tsm", "taiwan semiconductor"),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="TSMC",
                exchange_code="XTAI",
                display_name="TSMC",
                aliases=("taiwan semiconductor manufacturing company", "tsmc"),
                provider="massive",
            ),
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("TSMC")

    assert cached is False
    assert [item.ticker for item in results] == ["TSMC", "TSM"]


def test_lookup_ranking_is_stable_for_same_tier_using_provider_order() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FORD1",
                exchange_code="XNYS",
                display_name="Ford Alpha",
                aliases=("ford alpha",),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="FORD2",
                exchange_code="XNYS",
                display_name="Ford Bravo",
                aliases=("ford bravo",),
                provider="massive",
            ),
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Ford")

    assert cached is False
    assert [item.ticker for item in results] == ["FORD1", "FORD2"]


def test_lookup_cache_preserves_ranked_order() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="HIG",
                exchange_code="XNYS",
                display_name="The Hartford Financial Services Group, Inc.",
                aliases=("hartford",),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford", "ford motor company"),
                provider="massive",
            ),
        ],
    )
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    first_results, first_cached = service.lookup("Ford")
    second_results, second_cached = service.lookup("Ford")

    assert first_cached is False
    assert second_cached is True
    assert [item.ticker for item in first_results] == ["F", "HIG"]
    assert [item.ticker for item in second_results] == ["F", "HIG"]
    assert provider.search_calls == 1


def _now() -> datetime:
    return datetime.now(timezone.utc)
