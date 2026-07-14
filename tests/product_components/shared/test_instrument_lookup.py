from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock, patch

import psycopg

from src.product_components.shared.adapters import (
    SharedInstrumentSeed,
    SharedLookupCacheEntry,
    SharedWatchlistEntryInput,
    SharedWatchlistRecord,
)
from src.product_components.shared.instrument_lookup import (
    AlphaVantageInstrumentLookupProvider,
    InstrumentLookupSuggestion,
    MassiveInstrumentLookupProvider,
    OpenFigiInstrumentLookupProvider,
    SharedInstrumentLookupAdminService,
    _normalize_openfigi_results,
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
        self.upsert_calls: list[tuple[object, bool, bool]] = []

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

    def upsert_watchlist_entry(
        self,
        entry,
        *,
        replace_aliases: bool = True,
        enrich_aliases: bool = True,
    ):
        self.upsert_calls.append((entry, replace_aliases, enrich_aliases))
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

    results, cached, _warnings = service.lookup("nvidia")

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
    params = mock_get.call_args.kwargs["params"]
    assert params["limit"] == 50
    assert "market" not in params


def test_massive_provider_returns_otc_listed_foreign_stock() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "results": [
            {
                "ticker": "AXAHF",
                "primary_exchange": "OTC",
                "name": "AXA SA",
                "type": "ADRC",
            }
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("AXA")

    assert len(results) == 1
    assert results[0].ticker == "AXAHF"
    assert results[0].exchange_code == "OTC"


def test_alpha_vantage_provider_raises_on_rate_limit_response() -> None:
    provider = AlphaVantageInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        try:
            provider.search("Rheinmetall")
            raised = False
        except RuntimeError:
            raised = True

    assert raised


def test_lookup_enrichment_finds_primary_exchange_via_otc_display_name() -> None:
    """Polygon returns the OTC ADR listing; Alpha Vantage should be retried with
    the company name to surface the primary-exchange listing."""
    registry = FakeRegistry()
    admin = FakeAdmin()

    class OtcOnlyMassive(FakeProvider):
        def search(self, query: str) -> list[InstrumentLookupSuggestion]:
            self.search_calls += 1
            self.queries.append(query)
            if query in {"Rheinmetall", "rheinmetall"}:
                return [
                    InstrumentLookupSuggestion(
                        ticker="RNMBY",
                        exchange_code="OTC",
                        display_name="Rheinmetall AG",
                        aliases=("rheinmetall ag", "rnmby"),
                        provider="massive",
                    )
                ]
            return []

    class PrimaryExchangeAlphaVantage(FakeProvider):
        def search(self, query: str) -> list[InstrumentLookupSuggestion]:
            self.search_calls += 1
            self.queries.append(query)
            # Doesn't find it by short keyword but does by full company name
            if query == "Rheinmetall AG":
                return [
                    InstrumentLookupSuggestion(
                        ticker="RHM",
                        exchange_code="XETR",
                        display_name="Rheinmetall AG",
                        aliases=("rheinmetall ag", "rhm"),
                        provider="alpha_vantage",
                    )
                ]
            return []

    massive = OtcOnlyMassive(name="massive", results=[])
    alpha = PrimaryExchangeAlphaVantage(name="alpha_vantage", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(massive, alpha),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, _warnings = service.lookup("Rheinmetall")

    tickers = {r.ticker for r in results}
    assert "RNMBY" in tickers
    assert "RHM" in tickers
    assert any(r.exchange_code == "XETR" for r in results)
    assert "Rheinmetall AG" in alpha.queries  # enrichment query was tried


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
    assert "novo nordisk" in results[0].aliases
    assert "novo-nordisk" in results[0].aliases


def test_massive_provider_adds_curated_google_alias() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "results": [
            {
                "ticker": "GOOGL",
                "primary_exchange": "XNAS",
                "name": "Alphabet Inc. Class A Common Stock",
                "type": "CS",
            }
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Google")

    assert len(results) == 1
    assert "alphabet" in results[0].aliases
    assert "google" in results[0].aliases


def test_massive_provider_returns_foreign_stock_listed_as_depositary_shares() -> None:
    provider = MassiveInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "results": [
            {
                "ticker": "RNMBY",
                "primary_exchange": "OTC",
                "name": "Rheinmetall AG Depositary Shares",
                "type": "ADRC",
            }
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Rheinmetall")

    assert len(results) == 1
    assert results[0].ticker == "RNMBY"
    assert results[0].display_name == "Rheinmetall AG Depositary Shares"


def test_alpha_vantage_provider_returns_german_stock_with_frk_suffix() -> None:
    provider = AlphaVantageInstrumentLookupProvider(api_key="test-key")
    response = Mock()
    response.json.return_value = {
        "bestMatches": [
            {
                "1. symbol": "RHM.FRK",
                "2. name": "Rheinmetall AG",
                "3. type": "Equity",
                "4. region": "Germany",
                "8. currency": "EUR",
            }
        ]
    }
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.get", return_value=response):
        results = provider.search("Rheinmetall")

    assert len(results) == 1
    assert results[0].ticker == "RHM"
    assert results[0].exchange_code == "XETR"


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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("F")

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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("nvidia")

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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("Novo-Nordisk")

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

    results, cached, _warnings = service.lookup("Ford motor company")

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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("TSMC")

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

    results, cached, _warnings = service.lookup("F")

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

    results, cached, _warnings = service.lookup("F")

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

    results, cached, _warnings = service.lookup("Ford Motor")

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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("Ford")

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

    first_results, first_cached, _first_warnings = service.lookup("Ford")
    second_results, second_cached, _second_warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("NVDA")

    assert cached is True
    assert provider.search_calls == 0
    assert results[0].ticker == "NVDA"
    assert results[0].provider == "local_db"


def test_update_watchlist_entry_preserves_manual_alias_replacement() -> None:
    registry = FakeRegistry()
    registry.rows = []
    admin = FakeAdmin()
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    row = service.update_watchlist_entry(
        SharedWatchlistEntryInput(
            ticker="000660",
            exchange_code="XKRX",
            display_name="SK Hynix Inc.",
            aliases=("hynix",),
        )
    )

    assert admin.upsert_calls[0][2] is False
    assert row.aliases == ("hynix",)


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

    results, cached, _warnings = service.lookup("Ford")

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

    results, cached, _warnings = service.lookup("nvidia")

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

    results, cached, _warnings = service.lookup("NVDA", expand=True)

    assert cached is False
    assert provider.search_calls >= 1


def test_openfigi_normalize_maps_german_exchange_to_xetr() -> None:
    data = [
        {
            "figi": "BBG000BN3VB4",
            "name": "Rheinmetall AG",
            "ticker": "RHM",
            "exchCode": "GY",
            "securityType": "Common Stock",
            "marketSector": "Equity",
        }
    ]
    results = _normalize_openfigi_results(data, provider="openfigi")

    assert len(results) == 1
    assert results[0].ticker == "RHM"
    assert results[0].exchange_code == "XETR"
    assert results[0].display_name == "Rheinmetall AG"
    assert results[0].provider == "openfigi"


def test_openfigi_normalize_maps_french_exchange_to_xpar() -> None:
    data = [
        {
            "figi": "BBG000BC8029",
            "name": "AXA SA",
            "ticker": "CS",
            "exchCode": "FP",
            "securityType": "Common Stock",
            "marketSector": "Equity",
        }
    ]
    results = _normalize_openfigi_results(data, provider="openfigi")

    assert len(results) == 1
    assert results[0].ticker == "CS"
    assert results[0].exchange_code == "XPAR"


def test_openfigi_normalize_skips_unmapped_exchanges() -> None:
    data = [
        {
            "figi": "BBG000UNKNOWN",
            "name": "Some Stock",
            "ticker": "XYZ",
            "exchCode": "ZZ",
            "securityType": "Common Stock",
            "marketSector": "Equity",
        }
    ]
    results = _normalize_openfigi_results(data, provider="openfigi")

    assert len(results) == 0


def test_openfigi_normalize_filters_non_equity_results() -> None:
    data = [
        {
            "figi": "BBG000BOND01",
            "name": "Some Bond",
            "ticker": "BOND",
            "exchCode": "GY",
            "securityType": "Corp",
            "marketSector": "Corp",
        },
        {
            "figi": "BBG000FUT01",
            "name": "Some Future",
            "ticker": "RHMG=12",
            "exchCode": "GR",
            "securityType": "SINGLE STOCK FUTURE",
            "marketSector": "Equity",
        },
        {
            "figi": "BBG000STOCK1",
            "name": "Good Stock AG",
            "ticker": "GST",
            "exchCode": "GY",
            "securityType": "Common Stock",
            "marketSector": "Equity",
        },
    ]
    results = _normalize_openfigi_results(data, provider="openfigi")

    assert len(results) == 1
    assert results[0].ticker == "GST"


def test_openfigi_provider_posts_to_search_api() -> None:
    provider = OpenFigiInstrumentLookupProvider()
    response = Mock()
    response.json.return_value = {"data": []}
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.post", return_value=response) as mock_post:
        provider.search("Rheinmetall")

    assert mock_post.call_count == 1
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["json"] == {"query": "Rheinmetall"}
    assert "X-OPENFIGI-APIKEY" not in call_kwargs.kwargs["headers"]


def test_openfigi_provider_includes_api_key_header_when_configured() -> None:
    provider = OpenFigiInstrumentLookupProvider(api_key="test-figi-key")
    response = Mock()
    response.json.return_value = {"data": []}
    response.raise_for_status.return_value = None

    with patch("src.product_components.shared.instrument_lookup.requests.post", return_value=response) as mock_post:
        provider.search("Rheinmetall")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-OPENFIGI-APIKEY"] == "test-figi-key"


def test_run_search_reports_warning_for_provider_with_missing_api_key() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    provider_with_key = FakeProvider(
        name="massive",
        results=[
            InstrumentLookupSuggestion(
                ticker="F",
                exchange_code="XNYS",
                display_name="Ford Motor Company",
                aliases=("ford motor company", "f"),
                provider="massive",
            )
        ],
    )
    provider_no_key = AlphaVantageInstrumentLookupProvider(api_key="")
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider_with_key, provider_no_key),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, warnings = service.lookup("Ford")

    assert len(results) >= 1
    assert "alpha_vantage: API key not configured" in warnings


def test_run_search_reports_warning_for_provider_request_failure() -> None:
    registry = FakeRegistry()
    admin = FakeAdmin()
    failing_provider = FakeProvider(name="massive", results=[], raises=True)
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(failing_provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, warnings = service.lookup("Ford")

    assert len(results) == 0
    assert "massive: request failed" in warnings


def test_openfigi_provider_not_flagged_as_missing_key() -> None:
    """OpenFIGI works without an API key so it should not generate a missing-key warning."""
    registry = FakeRegistry()
    admin = FakeAdmin()
    openfigi = FakeProvider(
        name="openfigi",
        results=[
            InstrumentLookupSuggestion(
                ticker="RHM",
                exchange_code="XETR",
                display_name="Rheinmetall AG",
                aliases=("rheinmetall ag", "rhm"),
                provider="openfigi",
            )
        ],
    )
    openfigi._api_key = ""  # type: ignore[attr-defined]
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(openfigi,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, warnings = service.lookup("Rheinmetall")

    assert len(results) >= 1
    assert len(warnings) == 0


def test_local_results_returned_when_all_providers_fail_on_expand() -> None:
    """When external providers all fail during expand, local DB results should still be returned."""
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.local_db_seeds = [
        SharedInstrumentSeed(
            ticker="GLE",
            exchange_code="XPAR",
            display_name="Societe Generale SA",
            aliases=("societe generale", "gle"),
            identifiers={},
            enabled=True,
        )
    ]
    failing_provider = FakeProvider(name="massive", results=[], raises=True)
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(failing_provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, warnings = service.lookup("Societe Generale", expand=True)

    assert len(results) >= 1
    assert results[0].ticker == "GLE"
    assert "massive: request failed" in warnings


def test_local_results_survive_cached_provider_error() -> None:
    """After a provider error is cached, non-expanded lookups should still return local results."""
    registry = FakeRegistry()
    admin = FakeAdmin()
    admin.local_db_seeds = [
        SharedInstrumentSeed(
            ticker="GLE",
            exchange_code="XPAR",
            display_name="Societe Generale SA",
            aliases=("societe generale", "gle"),
            identifiers={},
            enabled=True,
        )
    ]
    admin.cache[("search", "societe generale")] = SharedLookupCacheEntry(
        operation="search",
        target="societe generale",
        provider="massive",
        payload={
            "results": [],
            "provider_error": True,
            "provider_warnings": ["massive: rate limit exceeded"],
        },
        fetched_at=_now(),
        expires_at=_now() + timedelta(seconds=60),
    )
    provider = FakeProvider(name="massive", results=[])
    service = SharedInstrumentLookupAdminService(
        registry=registry,
        admin=admin,
        providers=(provider,),
        lookup_cache_ttl_seconds=3600,
        alias_cache_ttl_seconds=3600,
    )

    results, cached, warnings = service.lookup("Societe Generale")

    assert cached is True
    assert len(results) >= 1
    assert results[0].ticker == "GLE"
    assert "massive: rate limit exceeded" in warnings
    assert provider.search_calls == 0


def _now() -> datetime:
    return datetime.now(timezone.utc)
