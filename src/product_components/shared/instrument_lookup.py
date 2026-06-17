from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any
from typing import Protocol
import threading
import time

import psycopg
import requests

from src.product_components.market_data.provider_symbols import normalize_exchange_code

logger = logging.getLogger(__name__)

_EMPTY_RESULT_CACHE_TTL_SECONDS = 60
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    SharedInstrumentSeed,
    SharedLookupCacheEntry,
    SharedWatchlistEntryInput,
    SharedWatchlistRecord,
)


@dataclass(frozen=True)
class InstrumentLookupSuggestion:
    ticker: str
    exchange_code: str
    display_name: str
    aliases: tuple[str, ...]
    provider: str

    def to_payload(self) -> dict:
        return {
            "ticker": self.ticker,
            "exchange_code": self.exchange_code,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "provider": self.provider,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "InstrumentLookupSuggestion":
        return cls(
            ticker=str(payload.get("ticker") or "").strip().upper(),
            exchange_code=str(payload.get("exchange_code") or "").strip().upper(),
            display_name=str(payload.get("display_name") or "").strip(),
            aliases=tuple(
                str(alias).strip().lower()
                for alias in payload.get("aliases", [])
                if str(alias).strip()
            ),
            provider=str(payload.get("provider") or "").strip(),
        )


class InstrumentLookupProvider(Protocol):
    provider_name: str

    def search(self, query: str) -> list[InstrumentLookupSuggestion]:
        ...

    def discover_aliases(
        self,
        *,
        ticker: str,
        exchange_code: str,
        display_name: str | None,
    ) -> InstrumentLookupSuggestion | None:
        ...


class MassiveInstrumentLookupProvider:
    provider_name = "massive"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.polygon.io",
        timeout_seconds: int = 10,
        search_limit: int = 50,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._search_limit = max(10, search_limit)

    def search(self, query: str) -> list[InstrumentLookupSuggestion]:
        if not self._api_key or not query.strip():
            return []
        exact_candidates: list[InstrumentLookupSuggestion] = []
        if _is_single_letter_ticker_query(query):
            try:
                exact_candidates = self._fetch_exact_ticker_candidates(query)
            except Exception:
                exact_candidates = []
        response = requests.get(
            f"{self._base_url}/v3/reference/tickers",
            params={
                "search": query.strip(),
                "market": "stocks",
                "active": "true",
                "limit": self._search_limit,
                "apiKey": self._api_key,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        search_candidates = _normalize_massive_results(payload.get("results"), provider=self.provider_name)
        return _merge_suggestions(exact_candidates, search_candidates)

    def discover_aliases(
        self,
        *,
        ticker: str,
        exchange_code: str,
        display_name: str | None,
    ) -> InstrumentLookupSuggestion | None:
        candidates = self.search(ticker)
        exact = _pick_exact_candidate(candidates, ticker=ticker, exchange_code=exchange_code)
        if exact is not None:
            return exact
        if display_name:
            candidates = self.search(display_name)
            exact = _pick_exact_candidate(candidates, ticker=ticker, exchange_code=exchange_code)
            if exact is not None:
                return exact
        return None

    def _fetch_exact_ticker_candidates(self, query: str) -> list[InstrumentLookupSuggestion]:
        response = requests.get(
            f"{self._base_url}/v3/reference/tickers/{query.strip().upper()}",
            params={
                "market": "stocks",
                "active": "true",
                "apiKey": self._api_key,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("results")
        if isinstance(result, dict):
            return _normalize_massive_results([result], provider=self.provider_name)
        return []


class AlphaVantageInstrumentLookupProvider:
    provider_name = "alpha_vantage"

    def __init__(self, *, api_key: str, timeout_seconds: int = 10) -> None:
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds

    def search(self, query: str) -> list[InstrumentLookupSuggestion]:
        if not self._api_key or not query.strip():
            return []
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "SYMBOL_SEARCH",
                "keywords": query.strip(),
                "apikey": self._api_key,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return _normalize_alpha_vantage_matches(payload.get("bestMatches"), provider=self.provider_name)

    def discover_aliases(
        self,
        *,
        ticker: str,
        exchange_code: str,
        display_name: str | None,
    ) -> InstrumentLookupSuggestion | None:
        candidates = self.search(ticker)
        exact = _pick_exact_candidate(candidates, ticker=ticker, exchange_code=exchange_code)
        if exact is not None:
            return exact
        if display_name:
            candidates = self.search(display_name)
            exact = _pick_exact_candidate(candidates, ticker=ticker, exchange_code=exchange_code)
            if exact is not None:
                return exact
        return None


class DuplicateActiveWatchlistEntry(ValueError):
    pass


@dataclass
class _PendingLookup:
    event: threading.Event
    suggestions: list[InstrumentLookupSuggestion] | None = None
    provider_name: str = "none"
    any_error: bool = False


class SharedInstrumentLookupAdminService:
    def __init__(
        self,
        *,
        registry: PostgresSharedInstrumentRegistry,
        admin: PostgresSharedInstrumentAdmin,
        providers: tuple[InstrumentLookupProvider, ...],
        lookup_cache_ttl_seconds: int,
        alias_cache_ttl_seconds: int,
        lookup_provider_debounce_ms: int = 0,
    ) -> None:
        self._registry = registry
        self._admin = admin
        self._providers = providers
        self._lookup_cache_ttl_seconds = lookup_cache_ttl_seconds
        self._alias_cache_ttl_seconds = alias_cache_ttl_seconds
        self._lookup_provider_debounce_seconds = max(0.0, lookup_provider_debounce_ms / 1000.0)
        self._pending_lookup_lock = threading.Lock()
        self._pending_lookups: dict[str, _PendingLookup] = {}

    def list_watchlist(self) -> list[SharedWatchlistRecord]:
        try:
            return self._registry.list_watchlist_records(active_only=True)
        except psycopg.Error:
            return []

    def lookup(self, query: str, *, expand: bool = False) -> tuple[list[InstrumentLookupSuggestion], bool]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            raise ValueError("lookup query must not be empty")
        cache_entry = None
        try:
            cache_entry = self._admin.load_lookup_cache(operation="search", target=normalized_query)
        except psycopg.Error:
            cache_entry = None
        cached_suggestions = _suggestions_from_cache(cache_entry) if cache_entry is not None else []
        cached_suggestions = _filter_query_specific_suggestions(query, cached_suggestions)
        if not expand:
            if cache_entry is not None and cache_entry.expires_at > _utc_now():
                if cached_suggestions and _cache_has_strong_match(query, cached_suggestions):
                    return cached_suggestions, True
                if not cached_suggestions and cache_entry.payload.get("provider_error"):
                    return [], True
            local_suggestions = self._search_local_db(query)
            if local_suggestions and _cache_has_strong_match(query, local_suggestions):
                return local_suggestions, True
        suggestions, provider_name, owns_lookup, any_error = self._run_search_with_coalescing(
            cache_key=normalized_query,
            query=query,
        )
        if owns_lookup:
            if suggestions:
                try:
                    self._admin.persist_lookup_results(tuple(
                        SharedInstrumentSeed(
                            ticker=s.ticker,
                            exchange_code=s.exchange_code,
                            display_name=s.display_name,
                            aliases=s.aliases,
                            identifiers={},
                            enabled=True,
                        )
                        for s in suggestions
                    ))
                except psycopg.Error:
                    pass
                try:
                    self._admin.save_lookup_cache(
                        operation="search",
                        target=normalized_query,
                        provider=provider_name,
                        payload={"results": [item.to_payload() for item in suggestions]},
                        fetched_at=_utc_now(),
                        expires_at=_utc_now() + timedelta(seconds=self._lookup_cache_ttl_seconds),
                    )
                except psycopg.Error:
                    pass
            elif any_error:
                try:
                    self._admin.save_lookup_cache(
                        operation="search",
                        target=normalized_query,
                        provider=provider_name,
                        payload={"results": [], "provider_error": True},
                        fetched_at=_utc_now(),
                        expires_at=_utc_now() + timedelta(seconds=_EMPTY_RESULT_CACHE_TTL_SECONDS),
                    )
                except psycopg.Error:
                    pass
        return suggestions, False

    def _search_local_db(self, query: str) -> list[InstrumentLookupSuggestion]:
        try:
            seeds = self._admin.search_instruments_locally(query)
        except psycopg.Error:
            return []
        if not seeds:
            return []
        suggestions = _suggestions_from_db_seeds(seeds)
        suggestions = _filter_query_specific_suggestions(query, suggestions)
        if not suggestions:
            return []
        return _rank_suggestions(query, suggestions)

    def discover_aliases(
        self,
        *,
        ticker: str,
        exchange_code: str,
        display_name: str | None,
    ) -> tuple[InstrumentLookupSuggestion | None, bool]:
        normalized_ticker = ticker.strip().upper()
        normalized_exchange = exchange_code.strip().upper()
        cache_key = f"{normalized_ticker}|{normalized_exchange}"
        cache_entry = None
        try:
            cache_entry = self._admin.load_lookup_cache(operation="alias_discovery", target=cache_key)
        except psycopg.Error:
            cache_entry = None
        if cache_entry is not None and cache_entry.expires_at > _utc_now():
            results = _suggestions_from_cache(cache_entry)
            return (results[0] if results else None), True
        suggestion: InstrumentLookupSuggestion | None = None
        provider_name = "none"
        for provider in self._providers:
            try:
                suggestion = provider.discover_aliases(
                    ticker=normalized_ticker,
                    exchange_code=normalized_exchange,
                    display_name=display_name,
                )
            except Exception:
                suggestion = None
            if suggestion is not None:
                provider_name = provider.provider_name
                break
        payload = {"results": [suggestion.to_payload()]} if suggestion is not None else {"results": []}
        try:
            self._admin.save_lookup_cache(
                operation="alias_discovery",
                target=cache_key,
                provider=provider_name,
                payload=payload,
                fetched_at=_utc_now(),
                expires_at=_utc_now() + timedelta(seconds=self._alias_cache_ttl_seconds),
            )
        except psycopg.Error:
            pass
        return suggestion, False

    def add_watchlist_entry(self, entry: SharedWatchlistEntryInput) -> SharedWatchlistRecord:
        existing = self._registry.get_watchlist_record(
            ticker=entry.ticker,
            exchange_code=entry.exchange_code,
        )
        if existing is not None and existing.is_active:
            raise DuplicateActiveWatchlistEntry(
                f"watchlist entry already active for {existing.ticker}:{existing.exchange_code}"
            )
        self._admin.upsert_watchlist_entry(entry, replace_aliases=True)
        return self._registry.get_watchlist_record(
            ticker=entry.ticker,
            exchange_code=entry.exchange_code,
        ) or SharedWatchlistRecord(
            ticker=entry.ticker.strip().upper(),
            exchange_code=entry.exchange_code.strip().upper(),
            display_name=entry.display_name.strip(),
            aliases=tuple(alias.strip().lower() for alias in entry.aliases if alias.strip()),
            is_active=True,
            source=entry.source,
        )

    def update_watchlist_entry(self, entry: SharedWatchlistEntryInput) -> SharedWatchlistRecord:
        self._admin.upsert_watchlist_entry(entry, replace_aliases=True)
        return self._registry.get_watchlist_record(
            ticker=entry.ticker,
            exchange_code=entry.exchange_code,
        ) or SharedWatchlistRecord(
            ticker=entry.ticker.strip().upper(),
            exchange_code=entry.exchange_code.strip().upper(),
            display_name=entry.display_name.strip(),
            aliases=tuple(alias.strip().lower() for alias in entry.aliases if alias.strip()),
            is_active=True,
            source=entry.source,
        )

    def deactivate_watchlist_entry(self, *, ticker: str, exchange_code: str) -> None:
        self._admin.deactivate_watchlist_entry(ticker=ticker, exchange_code=exchange_code)

    def _run_search(self, query: str) -> tuple[list[InstrumentLookupSuggestion], str, bool]:
        variants = _search_variants(query)
        last_provider = "none"
        collected_suggestions: list[InstrumentLookupSuggestion] = []
        seen_keys: set[tuple[str, str]] = set()
        any_error = False
        for provider in self._providers:
            last_provider = provider.provider_name
            provider_suggestions: list[InstrumentLookupSuggestion] = []
            for candidate_query in variants:
                try:
                    suggestions = provider.search(candidate_query)
                except Exception as exc:
                    logger.warning(
                        "provider %s search failed for query %r: %s",
                        provider.provider_name,
                        candidate_query,
                        exc,
                    )
                    suggestions = []
                    any_error = True
                if suggestions:
                    provider_suggestions = suggestions
                    break
            if not provider_suggestions:
                continue
            for suggestion in provider_suggestions:
                dedupe_key = (suggestion.ticker, normalize_exchange_code(suggestion.exchange_code))
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                collected_suggestions.append(suggestion)
        if not collected_suggestions:
            return [], last_provider, any_error
        ranked = _rank_suggestions(query, collected_suggestions)
        ranked = _filter_query_specific_suggestions(query, ranked)
        if not ranked:
            return [], last_provider, any_error
        top_provider = ranked[0].provider if ranked[0].provider else last_provider
        return ranked, top_provider, any_error

    def _run_search_with_coalescing(
        self,
        *,
        cache_key: str,
        query: str,
    ) -> tuple[list[InstrumentLookupSuggestion], str, bool, bool]:
        with self._pending_lookup_lock:
            pending = self._pending_lookups.get(cache_key)
            if pending is None:
                pending = _PendingLookup(event=threading.Event())
                self._pending_lookups[cache_key] = pending
                owns_lookup = True
            else:
                owns_lookup = False

        if not owns_lookup:
            pending.event.wait()
            return pending.suggestions or [], pending.provider_name, False, pending.any_error

        try:
            if self._lookup_provider_debounce_seconds > 0:
                time.sleep(self._lookup_provider_debounce_seconds)
            suggestions, provider_name, any_error = self._run_search(query)
            pending.suggestions = suggestions
            pending.provider_name = provider_name
            pending.any_error = any_error
            return suggestions, provider_name, True, any_error
        finally:
            pending.event.set()
            with self._pending_lookup_lock:
                self._pending_lookups.pop(cache_key, None)


def _suggestions_from_db_seeds(seeds: list[SharedInstrumentSeed]) -> list[InstrumentLookupSuggestion]:
    return _filter_supported_stock_suggestions([
        InstrumentLookupSuggestion(
            ticker=seed.ticker,
            exchange_code=seed.exchange_code,
            display_name=seed.display_name,
            aliases=seed.aliases,
            provider="local_db",
        )
        for seed in seeds
    ])


def _normalize_massive_results(results: object, *, provider: str) -> list[InstrumentLookupSuggestion]:
    if not isinstance(results, list):
        return []
    suggestions: list[InstrumentLookupSuggestion] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if not _is_supported_massive_stock_result(item):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        exchange_code = normalize_exchange_code(str(item.get("primary_exchange") or "").strip().upper())
        display_name = str(item.get("name") or ticker).strip()
        if not ticker or not exchange_code or not display_name:
            continue
        suggestions.append(
            InstrumentLookupSuggestion(
                ticker=ticker,
                exchange_code=exchange_code,
                display_name=display_name,
                aliases=_normalize_aliases((ticker, display_name, _base_company_name(display_name))),
                provider=provider,
            )
        )
    return _filter_supported_stock_suggestions(suggestions)


def _is_supported_massive_stock_result(item: dict[str, Any]) -> bool:
    security_type = str(item.get("type") or "").strip().upper()
    if not security_type:
        return True
    # Keep equity-like instruments and drop fixed-income results that can leak into the search API.
    return security_type not in {"BOND", "RB", "DBT"}


def _normalize_alpha_vantage_matches(matches: object, *, provider: str) -> list[InstrumentLookupSuggestion]:
    if not isinstance(matches, list):
        return []
    suggestions: list[InstrumentLookupSuggestion] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        raw_symbol = str(item.get("1. symbol") or "").strip().upper()
        display_name = str(item.get("2. name") or raw_symbol).strip()
        exchange_code = _alpha_vantage_exchange_code(raw_symbol)
        ticker = raw_symbol.split(".", 1)[0]
        if not ticker or not exchange_code or not display_name:
            continue
        suggestions.append(
            InstrumentLookupSuggestion(
                ticker=ticker,
                exchange_code=exchange_code,
                display_name=display_name,
                aliases=_normalize_aliases((ticker, display_name, _base_company_name(display_name))),
                provider=provider,
            )
        )
    return _filter_supported_stock_suggestions(suggestions)


def _alpha_vantage_exchange_code(raw_symbol: str) -> str:
    normalized = raw_symbol.strip().upper()
    if normalized.endswith(".DEX"):
        return "XETR"
    if normalized.endswith(".PAR"):
        return "XPAR"
    return ""


def _pick_exact_candidate(
    candidates: list[InstrumentLookupSuggestion],
    *,
    ticker: str,
    exchange_code: str,
) -> InstrumentLookupSuggestion | None:
    normalized_ticker = ticker.strip().upper()
    normalized_exchange = normalize_exchange_code(exchange_code.strip().upper())
    for candidate in candidates:
        if candidate.ticker == normalized_ticker and normalize_exchange_code(candidate.exchange_code) == normalized_exchange:
            return candidate
    return None


# Strips share-class designations ("Class A/B/C …") and common corporate-form
# suffixes ("Inc.", "Corp.", "Ltd.", etc.) from provider display names so that
# the plain company name is always available as a news-matching alias.
_CLASS_DESIGNATION = re.compile(r"\s+class\s+[a-z]\b.*$", re.IGNORECASE)
_CORPORATE_SUFFIX = re.compile(
    r"[\s,]+(?:inc|corp|ltd|plc|llc|s\.a|n\.v|a\/s|co|company|corporation|incorporated|limited)\.?\s*$",
    re.IGNORECASE,
)


def _base_company_name(display_name: str) -> str:
    """Return the plain company name without class designations or corporate suffixes."""
    name = _CLASS_DESIGNATION.sub("", display_name).strip()
    name = _CORPORATE_SUFFIX.sub("", name).strip()
    return name


def _normalize_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value.strip().lower()
                for value in values
                if value and value.strip()
            }
        )
    )


def _merge_suggestions(
    *suggestion_lists: list[InstrumentLookupSuggestion],
) -> list[InstrumentLookupSuggestion]:
    merged: list[InstrumentLookupSuggestion] = []
    seen_keys: set[tuple[str, str]] = set()
    for suggestions in suggestion_lists:
        for suggestion in suggestions:
            dedupe_key = (suggestion.ticker, normalize_exchange_code(suggestion.exchange_code))
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            merged.append(suggestion)
    return merged


def _rank_suggestions(
    query: str,
    suggestions: list[InstrumentLookupSuggestion],
) -> list[InstrumentLookupSuggestion]:
    return [
        suggestion
        for _rank_key, suggestion in sorted(
            (
                (_suggestion_rank_key(query, suggestion, index), suggestion)
                for index, suggestion in enumerate(suggestions)
            ),
            key=lambda item: item[0],
        )
    ]


def _suggestion_rank_key(
    query: str,
    suggestion: InstrumentLookupSuggestion,
    index: int,
) -> tuple[int, int, int, int]:
    query_tokens = _tokens(query)
    normalized_query = " ".join(query_tokens)
    if not normalized_query:
        return (6, 0, 0, index)

    best_rank = (6, 0, 10_000, index)
    for candidate in _candidate_strings(suggestion):
        rank = _match_rank(
            normalized_query,
            query_tokens,
            candidate["kind"],
            candidate["text"],
            candidate["tokens"],
            index,
        )
        if rank < best_rank:
            best_rank = rank
    return best_rank


def _candidate_strings(suggestion: InstrumentLookupSuggestion) -> list[dict[str, Any]]:
    values = [
        ("ticker", suggestion.ticker),
        ("display_name", suggestion.display_name),
        *[("alias", alias) for alias in suggestion.aliases],
    ]
    candidates: list[dict[str, Any]] = []
    for kind, value in values:
        normalized = _normalize_match_text(value)
        if not normalized:
            continue
        candidates.append(
            {
                "kind": kind,
                "text": normalized,
                "tokens": _tokens(normalized),
            }
        )
    return candidates


def _match_rank(
    normalized_query: str,
    query_tokens: list[str],
    candidate_kind: str,
    candidate_text: str,
    candidate_tokens: list[str],
    index: int,
) -> tuple[int, int, int, int]:
    field_length = len(candidate_text)
    extra_tokens = max(0, len(candidate_tokens) - len(query_tokens))
    query_is_single_word = len(query_tokens) == 1

    if candidate_text == normalized_query:
        if candidate_kind == "ticker":
            return (0, -field_length, extra_tokens, index)
        if query_is_single_word:
            return (1, -field_length, extra_tokens, index)
        return (2, -field_length, extra_tokens, index)

    if query_is_single_word and normalized_query in candidate_tokens:
        return (1, -field_length, extra_tokens, index)

    if not query_is_single_word and _contains_exact_token_sequence(query_tokens, candidate_tokens):
        return (2, -field_length, extra_tokens, index)

    if _has_prefix_token_match(normalized_query, candidate_tokens):
        return (3, -field_length, extra_tokens, index)

    if normalized_query in candidate_text:
        return (4, -field_length, extra_tokens, index)

    return (6, -field_length, extra_tokens, index)


def _contains_exact_token_sequence(query_tokens: list[str], candidate_tokens: list[str]) -> bool:
    if not query_tokens or len(query_tokens) > len(candidate_tokens):
        return False
    query_length = len(query_tokens)
    for start in range(len(candidate_tokens) - query_length + 1):
        if candidate_tokens[start : start + query_length] == query_tokens:
            return True
    return False


def _has_prefix_token_match(normalized_query: str, candidate_tokens: list[str]) -> bool:
    if not normalized_query:
        return False
    return any(token.startswith(normalized_query) for token in candidate_tokens)


def _search_variants(query: str) -> tuple[str, ...]:
    raw = query.strip()
    normalized = _normalize_match_text(raw)
    if not raw:
        return ()
    variants: list[str] = []
    seen: set[str] = set()

    def _append(value: str) -> None:
        candidate = value.strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        variants.append(candidate)

    _append(raw)
    _append(normalized)

    normalized_tokens = normalized.split(" ") if normalized else []
    for end in range(len(normalized_tokens) - 1, 0, -1):
        _append(" ".join(normalized_tokens[:end]))

    return tuple(variants)


def _is_single_letter_ticker_query(query: str) -> bool:
    normalized = query.strip().upper()
    return len(normalized) == 1 and normalized.isalpha()


def _normalize_match_text(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[\u2010-\u2015_\-./]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _tokens(value: str) -> list[str]:
    normalized = _normalize_match_text(value)
    if not normalized:
        return []
    return normalized.split(" ")


def _suggestions_from_cache(entry: SharedLookupCacheEntry) -> list[InstrumentLookupSuggestion]:
    payload_results = entry.payload.get("results")
    if not isinstance(payload_results, list):
        return []
    return _filter_supported_stock_suggestions([
        InstrumentLookupSuggestion.from_payload(item)
        for item in payload_results
        if isinstance(item, dict)
    ])


def _filter_supported_stock_suggestions(
    suggestions: list[InstrumentLookupSuggestion],
) -> list[InstrumentLookupSuggestion]:
    return [suggestion for suggestion in suggestions if _is_supported_stock_suggestion(suggestion)]


def _filter_query_specific_suggestions(
    query: str,
    suggestions: list[InstrumentLookupSuggestion],
) -> list[InstrumentLookupSuggestion]:
    normalized_query = _normalize_match_text(query)
    if len(normalized_query) != 1:
        return suggestions
    return [
        suggestion
        for suggestion in suggestions
        if _normalize_match_text(suggestion.ticker) == normalized_query
    ]


def _is_supported_stock_suggestion(suggestion: InstrumentLookupSuggestion) -> bool:
    name = _normalize_match_text(suggestion.display_name)
    if not name:
        return False
    blocked_phrases = (
        " etf",
        " bond",
        " notes due",
        " note due",
        " preferred stock",
        " preferred shares",
        " depositary shares",
        " depositary share",
        " term preferred stock",
        " cumulative redeemable preferred stock",
        " non cumulative preferred stock",
    )
    if any(phrase in f" {name}" for phrase in blocked_phrases):
        return False
    if "%" in suggestion.display_name:
        return False
    return True


def _cache_has_strong_match(query: str, suggestions: list[InstrumentLookupSuggestion]) -> bool:
    return any(_suggestion_rank_key(query, suggestion, index)[0] <= 2 for index, suggestion in enumerate(suggestions))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
