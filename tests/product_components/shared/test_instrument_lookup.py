from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock, patch

import psycopg

from src.product_components.shared.adapters import (
    SharedInstrumentSeed,
    SharedLookupCacheEntry,
    SharedWatchlistRecord,
)
from src.product_components.shared.instrument_lookup import (
    AlphaVantageInstrumentLookupProvider,
    InstrumentLookupSuggestion,
    MassiveInstrumentLookupProvider,
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
        self.persisted_seeds: list = []
        self.local_db_seeds: list = []

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

    def persist_lookup_results(self, seeds):
        self.persisted_seeds.extend(seeds)

    def search_instruments_locally(self, query: str, *, limit: int = 50):
        return self.local_db_seeds


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


def test_massive_provider_requests_broader_candidate_set_for_ambiguous_queries() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {"results": []}
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response) as mock_get:
        provider.search("Ford")

    assert mock_get.call_count == 1
    assert mock_get.call_args.kwargs["params"]["limit"] == 50


def test_massive_provider_filters_bond_results_from_stock_suggestions() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "results": [
            {
                "ticker": "F-BOND",
                "primary_exchange": "XNYS",
                "name": "Ford Motor Company Bond",
                "type": "BOND",
            },
            {
                "ticker": "F",
                "primary_exchange": "XNYS",
                "name": "Ford Motor Company",
                "type": "CS",
            },
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Ford")

    assert [item.ticker for item in results] == ["F"]


def test_massive_provider_fetches_exact_single_letter_ticker_candidate() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    exact_response = Mock()
    exact_response.json.return_value = {
        "results": {
            "ticker": "F",
            "primary_exchange": "XNYS",
            "name": "Ford Motor Company",
            "type": "CS",
        }
    }
    exact_response.raise_for_status.return_value = None
    search_response = Mock()
    search_response.json.return_value = {
        "results": [
            {
                "ticker": "ACDC",
                "primary_exchange": "XNAS",
                "name": "ProFrac Holding Corp. Class A Common Stock",
                "type": "CS",
            }
        ]
    }
    search_response.raise_for_status.return_value = None

    with patch(
        "src.product_components.shared.instrument_lookup.requests.get",
        side_effect=[exact_response, search_response],
    ) as mock_get:
        results = provider.search("F")

    assert mock_get.call_count == 2
    assert mock_get.call_args_list[0].args[0].endswith("/v3/reference/tickers/F")
    assert [item.ticker for item in results] == ["F", "ACDC"]


def test_massive_provider_single_letter_lookup_falls_back_when_exact_fetch_fails() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    search_response = Mock()
    search_response.json.return_value = {
        "results": [
            {
                "ticker": "ACDC",
                "primary_exchange": "XNAS",
                "name": "ProFrac Holding Corp. Class A Common Stock",
                "type": "CS",
            }
        ]
    }
    search_response.raise_for_status.return_value = None

    with patch(
        "src.product_components.shared.instrument_lookup.requests.get",
        side_effect=[RuntimeError("exact lookup failed"), search_response],
    ):
        results = provider.search("F")

    assert [item.ticker for item in results] == ["ACDC"]


def test_alpha_vantage_provider_returns_us_stock() -> None:
    provider = AlphaVantageInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "bestMatches": [
            {
                "1. symbol": "NVO",
                "2. name": "Novo-Nordisk A/S",
                "3. type": "Equity",
                "4. region": "United States",
                "8. currency": "USD",
            }
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Novo Nordisk")

    assert len(results) == 1
    assert results[0].ticker == "NVO"
    assert results[0].exchange_code == "XNYS"
    assert results[0].display_name == "Novo-Nordisk A/S"


def test_alpha_vantage_provider_filters_unsupported_exchange() -> None:
    provider = AlphaVantageInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "bestMatches": [
            {
                "1. symbol": "NOVO-B.CO",
                "2. name": "Novo-Nordisk A/S",
                "3. type": "Equity",
                "4. region": "Denmark",
                "8. currency": "DKK",
            },
            {
                "1. symbol": "NVO",
                "2. name": "Novo-Nordisk A/S",
                "3. type": "Equity",
                "4. region": "United States",
                "8. currency": "USD",
            },
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Novo Nordisk")

    assert [item.ticker for item in results] == ["NVO"]


def test_lookup_cache_filters_non_stock_cached_results() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.cache[("search", "ford")] = SharedLookupCacheEntry(
        operation="search",
        target="ford",
        provider="massive",
        payload={
            "results": [
                {
                    "ticker": "FPC",
                    "exchange_code": "XNYS",
                    "display_name": "Ford Motor Company 6.000% Notes due December 1, 2059",
                    "aliases": ["ford motor company 6.000% notes due december 1, 2059", "fpc"],
                    "provider": "massive",
                },
                {
                    "ticker": "F",
                    "exchange_code": "XNYS",
                    "display_name": "Ford Motor Company",
                    "aliases": ["f", "ford motor company"],
                    "provider": "massive",
                },
                {
                    "ticker": "HTAB",
                    "exchange_code": "ARCX",
                    "display_name": "Hartford Schroders Tax-Aware Bond ETF",
                    "aliases": ["hartford schroders tax-aware bond etf", "htab"],
                    "provider": "massive",
                },
            ]
        },
        fetched_at=_now(),
        expires_at=_now() + timedelta(hours=1),
    )
    provider = FakeProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Ford")

    assert cached is True
    assert provider.search_calls == 0
    assert [item.ticker for item in results] == ["F"]


def test_lookup_single_letter_cache_returns_only_exact_ticker_matches() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.cache[("search", "f")] = SharedLookupCacheEntry(
        operation="search",
        target="f",
        provider="massive",
        payload={
            "results": [
                {
                    "ticker": "ACDC",
                    "exchange_code": "XNYS",
                    "display_name": "ProFrac Holding Corp. Class A Common Stock",
                    "aliases": ["profrac holding corp class a common stock", "acdc"],
                    "provider": "massive",
                },
                {
                    "ticker": "F",
                    "exchange_code": "XNYS",
                    "display_name": "Ford Motor Company",
                    "aliases": ["f", "ford motor company"],
                    "provider": "massive",
                },
            ]
        },
        fetched_at=_now(),
        expires_at=_now() + timedelta(hours=1),
    )
    provider = FakeProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("F")

    assert cached is True
    assert provider.search_calls == 0
    assert [item.ticker for item in results] == ["F"]


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


def test_lookup_refetches_weak_cached_results_to_surface_better_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.cache[("search", "ford")] = SharedLookupCacheEntry(
        operation="search",
        target="ford",
        provider="massive",
        payload={
            "results": [
                {
                    "ticker": "FND",
                    "exchange_code": "XNYS",
                    "display_name": "Founders Bank",
                    "aliases": ["founders bank"],
                    "provider": "massive",
                }
            ]
        },
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


def test_lookup_merges_fallback_results_when_primary_results_are_partial() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    primary = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FND",
                exchange_code="XNYS",
                display_name="Founders Bank",
                aliases=("founders bank",),
                provider="massive",
            )
        ],
    )
    fallback = FakeProvider(
        name="alpha_vantage",
        results=[
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford", "ford motor company"),
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

    results, cached = service.lookup("Ford")

    assert cached is False
    assert primary.search_calls == 1
    assert fallback.search_calls == 1
    assert [item.ticker for item in results] == ["F", "FND"]
    assert admin.saved_provider == "alpha_vantage"


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


def test_lookup_retries_with_shorter_multi_word_query_variants() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()

    class PhraseFallbackProvider(FakeProvider):
        def search(self, query: str) -> list[InstrumentLookupSuggestion]:
            self.search_calls += 1
            self.queries.append(query)
            if query == "ford motor":
                return [
                    InstrumentLookupSuggestion(
                        ticker="F",
                        exchange_code="XNYS",
                        display_name="Ford Motor Company",
                        aliases=("ford", "ford motor company"),
                        provider="massive",
                    )
                ]
            return []

    provider = PhraseFallbackProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("Ford motor company")

    assert cached is False
    assert provider.queries == ["Ford motor company", "ford motor company", "ford motor"]
    assert [item.ticker for item in results] == ["F"]


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


def test_lookup_ranks_exact_single_letter_ticker_match_first() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FORD",
                exchange_code="XNYS",
                display_name="Forward Industries, Inc.",
                aliases=("forward industries",),
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

    results, cached = service.lookup("F")

    assert cached is False
    assert [item.ticker for item in results] == ["F"]


def test_lookup_single_letter_query_returns_only_exact_ticker_matches() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FORD",
                exchange_code="XNYS",
                display_name="Forward Industries, Inc.",
                aliases=("forward industries",),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford", "ford motor company"),
                provider="massive",
            ),
            InstrumentLookupSuggestion(
                ticker="FI",
                exchange_code="XNYS",
                display_name="Fiserv, Inc.",
                aliases=("fiserv",),
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

    results, cached = service.lookup("F")

    assert cached is False
    assert [item.ticker for item in results] == ["F"]


def test_lookup_ranks_exact_phrase_match_above_single_word_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FMC",
                exchange_code="XNYS",
                display_name="Ford Manufacturing Capital",
                aliases=("ford",),
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

    results, cached = service.lookup("Ford Motor")

    assert cached is False
    assert [item.ticker for item in results] == ["F", "FMC"]


def test_lookup_prefers_longer_exact_word_match_within_same_rank_tier() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="FS",
                exchange_code="XNYS",
                display_name="Ford Short",
                aliases=("ford short",),
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
    assert [item.ticker for item in results] == ["F", "FS"]


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


def test_lookup_returns_local_db_result_when_strong_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.local_db_seeds = [
        SharedInstrumentSeed(
            ticker="NVDA",
            exchange_code="XNAS",
            display_name="NVIDIA Corporation",
            aliases=("nvidia", "nvidia corporation"),
            identifiers={},
            enabled=True,
        )
    ]
    provider = FakeProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached = service.lookup("NVDA")

    assert cached is True
    assert provider.search_calls == 0
    assert results[0].ticker == "NVDA"
    assert results[0].provider == "local_db"


def test_lookup_falls_through_to_provider_on_weak_local_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    # Weak match: "founders bank" doesn't strongly match query "ford"
    admin.local_db_seeds = [
        SharedInstrumentSeed(
            ticker="FND",
            exchange_code="XNYS",
            display_name="Founders Bank",
            aliases=("founders bank",),
            identifiers={},
            enabled=True,
        )
    ]
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
    assert provider.search_calls >= 1
    assert results[0].ticker == "F"


def test_lookup_persists_provider_results_to_local_db() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
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

    service.lookup("Ford")

    assert len(admin.persisted_seeds) == 1
    assert admin.persisted_seeds[0].ticker == "F"


def test_lookup_local_db_error_falls_through_to_provider() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()

    def broken_search(query: str, *, limit: int = 50):
        raise psycopg.errors.ConnectionTimeout("timeout")

    admin.search_instruments_locally = broken_search  # type: ignore[method-assign]
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="NVDA",
                exchange_code="XNAS",
                display_name="NVIDIA Corporation",
                aliases=("nvidia",),
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

    results, cached = service.lookup("nvidia")

    assert cached is False
    assert provider.search_calls >= 1
    assert results[0].ticker == "NVDA"


def test_lookup_expand_bypasses_local_db_strong_match() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.local_db_seeds = [
        SharedInstrumentSeed(
            ticker="NVDA",
            exchange_code="XNAS",
            display_name="NVIDIA Corporation",
            aliases=("nvidia", "nvidia corporation"),
            identifiers={},
            enabled=True,
        )
    ]
    provider = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="NVDA",
                exchange_code="XNAS",
                display_name="NVIDIA Corporation",
                aliases=("nvidia",),
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

    results, cached = service.lookup("NVDA", expand=True)

    assert cached is False
    assert provider.search_calls >= 1


def _now() -> datetime:
    return datetime.now(timezone.utc)
