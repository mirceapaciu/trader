from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from typing import Protocol
import threading
import time

import psycopg
import requests

from src.product_components.market_data.provider_symbols import normalize_exchange_code
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
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
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def search(self, query: str) -> list[InstrumentLookupSuggestion]:
        if not self._api_key or not query.strip():
            return []
        response = requests.get(
            f"{self._base_url}/v3/reference/tickers",
            params={
                "search": query.strip(),
                "market": "stocks",
                "active": "true",
                "limit": 10,
                "apiKey": self._api_key,
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return _normalize_massive_results(payload.get("results"), provider=self.provider_name)

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

    def lookup(self, query: str) -> tuple[list[InstrumentLookupSuggestion], bool]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            raise ValueError("lookup query must not be empty")
        cache_entry = None
        try:
            cache_entry = self._admin.load_lookup_cache(operation="search", target=normalized_query)
        except psycopg.Error:
            cache_entry = None
        cached_suggestions = _suggestions_from_cache(cache_entry) if cache_entry is not None else []
        if cache_entry is not None and cache_entry.expires_at > _utc_now() and cached_suggestions:
            return cached_suggestions, True
        suggestions, provider_name, owns_lookup = self._run_search_with_coalescing(
            cache_key=normalized_query,
            query=query,
        )
        if suggestions and owns_lookup:
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
        return suggestions, False

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

    def _run_search(self, query: str) -> tuple[list[InstrumentLookupSuggestion], str]:
        variants = _search_variants(query)
        last_provider = "none"
        for provider in self._providers:
            last_provider = provider.provider_name
            for candidate_query in variants:
                try:
                    suggestions = provider.search(candidate_query)
                except Exception:
                    suggestions = []
                if suggestions:
                    return _rank_suggestions(query, suggestions), last_provider
        return [], last_provider

    def _run_search_with_coalescing(
        self,
        *,
        cache_key: str,
        query: str,
    ) -> tuple[list[InstrumentLookupSuggestion], str, bool]:
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
            return pending.suggestions or [], pending.provider_name, False

        try:
            if self._lookup_provider_debounce_seconds > 0:
                time.sleep(self._lookup_provider_debounce_seconds)
            suggestions, provider_name = self._run_search(query)
            pending.suggestions = suggestions
            pending.provider_name = provider_name
            return suggestions, provider_name, True
        finally:
            pending.event.set()
            with self._pending_lookup_lock:
                self._pending_lookups.pop(cache_key, None)


def _normalize_massive_results(results: object, *, provider: str) -> list[InstrumentLookupSuggestion]:
    if not isinstance(results, list):
        return []
    suggestions: list[InstrumentLookupSuggestion] = []
    for item in results:
        if not isinstance(item, dict):
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
                aliases=_normalize_aliases((ticker, display_name)),
                provider=provider,
            )
        )
    return suggestions


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
                aliases=_normalize_aliases((ticker, display_name)),
                provider=provider,
            )
        )
    return suggestions


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

    best_rank = (6, 10_000, 10_000, index)
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

    if candidate_text == normalized_query:
        if candidate_kind == "ticker":
            return (0, field_length, extra_tokens, index)
        if candidate_kind == "alias":
            return (1, field_length, extra_tokens, index)
        return (2, field_length, extra_tokens, index)

    if _contains_exact_token_sequence(query_tokens, candidate_tokens):
        return (3, field_length, extra_tokens, index)

    if _has_prefix_token_match(normalized_query, candidate_tokens):
        return (4, field_length, extra_tokens, index)

    if normalized_query in candidate_text:
        return (5, field_length, extra_tokens, index)

    return (6, field_length, extra_tokens, index)


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
    if normalized and normalized != raw:
        return (raw, normalized)
    return (raw,)


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
    return [
        InstrumentLookupSuggestion.from_payload(item)
        for item in payload_results
        if isinstance(item, dict)
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
