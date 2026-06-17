from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Json

from src.product_components.shared.adapters import PostgresSharedInstrumentAdmin, SharedInstrumentSeed

from .settings import NewsFetcherSettings

_SOURCE_KEY = re.compile(r"^rss:[a-z0-9_:-]+$|^[a-z][a-z0-9_:-]*$")


@dataclass(frozen=True)
class InstrumentConfig:
    ticker: str
    exchange_code: str
    names: tuple[str, ...]
    aliases: tuple[str, ...]
    identifiers: dict[str, Any]
    enabled: bool


@dataclass(frozen=True)
class RssSourceConfig:
    source_key: str
    source_type: str
    base_url: str
    symbol_param: str
    default_query_params: dict[str, str]
    grouping_mode: str
    max_symbols_per_request: int
    min_request_interval_seconds: int
    enabled: bool
    symbol_mappings: tuple["RssSymbolMappingConfig", ...] = ()


@dataclass(frozen=True)
class RssSymbolMappingConfig:
    source_key: str
    ticker: str
    exchange_code: str
    provider_symbol: str
    query_params: dict[str, str]
    enabled: bool


def sync_seed_configs(*, repo_root: Path, settings: NewsFetcherSettings) -> None:
    instruments = load_instruments_config(settings.instruments_config_path(repo_root))
    rss_sources = load_rss_sources_config(settings.rss_sources_config_path(repo_root))
    legacy_sources = _legacy_rss_sources(settings)

    instrument_admin = PostgresSharedInstrumentAdmin(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
    )
    _sync_instruments(
        instrument_admin=instrument_admin,
        instruments=instruments,
    )

    with psycopg.connect(settings.postgres_dsn) as conn:
        _sync_rss_sources(
            conn=conn,
            news_schema=settings.newsfetcher_db_schema,
            sources=(*rss_sources, *legacy_sources),
        )
        conn.commit()


def load_instruments_config(path: Path | None) -> tuple[InstrumentConfig, ...]:
    if path is None or not path.exists():
        return ()
    return parse_instruments_config(_load_json(path), source=str(path))


def parse_instruments_config(payload: dict[str, Any], *, source: str = "<memory>") -> tuple[InstrumentConfig, ...]:
    instruments = payload.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError(f"{source} must contain an instruments list")

    parsed: list[InstrumentConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in instruments:
        if not isinstance(item, dict):
            raise ValueError(f"{source} contains a non-object instrument")
        ticker = _required_text(item, "ticker").upper()
        exchange_code = _required_text(item, "exchange_code").upper()
        key = (ticker, exchange_code)
        if key in seen:
            raise ValueError(f"Duplicate instrument {ticker}/{exchange_code} in {source}")
        seen.add(key)
        parsed.append(
            InstrumentConfig(
                ticker=ticker,
                exchange_code=exchange_code,
                names=tuple(_string_list(item.get("names"))),
                aliases=tuple(_string_list(item.get("aliases"))),
                identifiers=item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {},
                enabled=bool(item.get("enabled", True)),
            )
        )
    return tuple(parsed)


def load_rss_sources_config(path: Path | None) -> tuple[RssSourceConfig, ...]:
    if path is None or not path.exists():
        return ()
    return parse_rss_sources_config(_load_json(path), source=str(path))


def parse_rss_sources_config(payload: dict[str, Any], *, source: str = "<memory>") -> tuple[RssSourceConfig, ...]:
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"{source} must contain a sources list")

    parsed: list[RssSourceConfig] = []
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError(f"{source} contains a non-object source")
        source_key = _required_text(item, "source_key")
        _validate_source_key(source_key)
        if source_key in seen:
            raise ValueError(f"Duplicate source_key {source_key!r} in {source}")
        seen.add(source_key)

        source_type = _required_text(item, "type")
        if source_type == "static":
            base_url = _required_text(item, "url")
            grouping_mode = "static"
            symbol_param = ""
            max_symbols = 1
            mappings: tuple[RssSymbolMappingConfig, ...] = ()
        elif source_type == "dynamic_watchlist":
            base_url = _required_text(item, "base_url")
            grouping_mode = str(item.get("grouping_mode") or "grouped")
            symbol_param = str(item.get("symbol_param") or "s")
            max_symbols = int(item.get("max_symbols_per_request") or 10)
            mappings = tuple(_parse_symbol_mappings(source_key, item.get("symbol_mappings"), source))
        else:
            raise ValueError(f"Unsupported RSS source type {source_type!r} in {source}")

        parsed.append(
            RssSourceConfig(
                source_key=source_key,
                source_type=source_type,
                base_url=base_url,
                symbol_param=symbol_param,
                default_query_params=_string_dict(item.get("default_query_params")),
                grouping_mode=grouping_mode,
                max_symbols_per_request=max_symbols,
                min_request_interval_seconds=int(item.get("min_request_interval_seconds") or 900),
                enabled=bool(item.get("enabled", True)),
                symbol_mappings=mappings,
            )
        )
    return tuple(parsed)


def _parse_symbol_mappings(
    source_key: str,
    value: Any,
    source: str,
) -> list[RssSymbolMappingConfig]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"symbol_mappings for {source_key!r} in {source} must be a list")
    mappings: list[RssSymbolMappingConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"symbol_mappings for {source_key!r} in {source} contains a non-object")
        ticker = _required_text(item, "ticker").upper()
        exchange_code = _required_text(item, "exchange_code").upper()
        key = (ticker, exchange_code)
        if key in seen:
            raise ValueError(f"Duplicate symbol mapping {source_key}:{ticker}/{exchange_code} in {source}")
        seen.add(key)
        mappings.append(
            RssSymbolMappingConfig(
                source_key=source_key,
                ticker=ticker,
                exchange_code=exchange_code,
                provider_symbol=_required_text(item, "provider_symbol").upper(),
                query_params=_string_dict(item.get("query_params")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return mappings


def _sync_instruments(
    *,
    instrument_admin: PostgresSharedInstrumentAdmin,
    instruments: tuple[InstrumentConfig, ...],
) -> None:
    if not instruments:
        return
    instrument_admin.upsert_instruments(
        tuple(
            SharedInstrumentSeed(
                ticker=instrument.ticker,
                exchange_code=instrument.exchange_code,
                display_name=instrument.names[0] if instrument.names else instrument.ticker,
                aliases=(*instrument.names, *instrument.aliases),
                identifiers=instrument.identifiers,
                enabled=instrument.enabled,
                isin=instrument.identifiers.get("isin") or None,
            )
            for instrument in instruments
        )
    )


def _sync_rss_sources(
    *,
    conn: psycopg.Connection,
    news_schema: str,
    sources: tuple[RssSourceConfig, ...],
) -> None:
    if not sources:
        return
    with conn.cursor() as cur:
        for source in sources:
            cur.execute(
                f"""
                INSERT INTO {news_schema}.t_rss_sources
                    (source_key, base_url, source_type, symbol_param, default_query_params,
                     max_symbols_per_request, min_request_interval_seconds, grouping_mode,
                     is_enabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (source_key)
                DO UPDATE SET
                    base_url = EXCLUDED.base_url,
                    source_type = EXCLUDED.source_type,
                    symbol_param = EXCLUDED.symbol_param,
                    default_query_params = EXCLUDED.default_query_params,
                    max_symbols_per_request = EXCLUDED.max_symbols_per_request,
                    min_request_interval_seconds = EXCLUDED.min_request_interval_seconds,
                    grouping_mode = EXCLUDED.grouping_mode,
                    is_enabled = EXCLUDED.is_enabled,
                    updated_at = NOW()
                """,
                (
                    source.source_key,
                    source.base_url,
                    source.source_type,
                    source.symbol_param,
                    Json(source.default_query_params),
                    source.max_symbols_per_request,
                    source.min_request_interval_seconds,
                    source.grouping_mode,
                    source.enabled,
                ),
            )
            for mapping in source.symbol_mappings:
                cur.execute(
                    f"""
                    INSERT INTO {news_schema}.t_rss_symbol_rules
                        (source_key, ticker, exchange_code, provider_symbol, query_params,
                         match_terms, is_enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, '[]'::jsonb, %s, NOW(), NOW())
                    ON CONFLICT (source_key, ticker, exchange_code)
                    DO UPDATE SET
                        provider_symbol = EXCLUDED.provider_symbol,
                        query_params = EXCLUDED.query_params,
                        match_terms = '[]'::jsonb,
                        is_enabled = EXCLUDED.is_enabled,
                        updated_at = NOW()
                    """,
                    (
                        mapping.source_key,
                        mapping.ticker,
                        mapping.exchange_code,
                        mapping.provider_symbol,
                        Json(mapping.query_params),
                        mapping.enabled,
                    ),
                )


def _legacy_rss_sources(settings: NewsFetcherSettings) -> tuple[RssSourceConfig, ...]:
    sources: list[RssSourceConfig] = []
    for url in settings.legacy_rss_feed_urls:
        sources.append(
            RssSourceConfig(
                source_key=_static_source_key(url),
                source_type="static",
                base_url=url,
                symbol_param="",
                default_query_params={},
                grouping_mode="static",
                max_symbols_per_request=1,
                min_request_interval_seconds=settings.rss_rate_limit_backoff_seconds,
                enabled=True,
            )
        )
    return tuple(sources)


def _static_source_key(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    host_label = host.split(".")[0] or "rss"
    path_parts = [part for part in parsed.path.split("/") if part]
    path_label = path_parts[-1] if path_parts else "feed"
    raw = f"rss:{host_label}:{path_label}"
    normalized = re.sub(r"[^a-z0-9:]+", "_", raw.lower()).strip("_")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    return f"{normalized}:{digest}" if normalized == "rss:rss:feed" else normalized


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required text field {key!r}")
    return value.strip()


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key).strip() and str(item).strip()}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _validate_source_key(source_key: str) -> None:
    if not _SOURCE_KEY.match(source_key):
        raise ValueError(f"Invalid RSS source_key: {source_key!r}")
