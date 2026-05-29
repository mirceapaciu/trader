from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class WatchlistTicker:
    ticker: str
    exchange_code: str


@dataclass(frozen=True)
class RssTickerMatch:
    ticker: str
    match_terms: tuple[str, ...]


@dataclass(frozen=True)
class RssFeedSpec:
    source_key: str
    provider_name: str
    url: str
    app_tickers: tuple[str, ...] = ()
    ticker_matches: tuple[RssTickerMatch, ...] = ()
    min_request_interval_seconds: int = 0


def build_rss_feed_url(
    *,
    base_url: str,
    symbol_param: str,
    provider_symbol: str | tuple[str, ...],
    query_params: dict[str, str],
) -> str:
    parsed = urlsplit(base_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if isinstance(provider_symbol, tuple):
        params[symbol_param] = ",".join(provider_symbol)
    else:
        params[symbol_param] = provider_symbol
    params.update(query_params)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(params, safe=","),
            parsed.fragment,
        )
    )


def rss_source_key(*, provider_name: str, ticker: str, exchange_code: str) -> str:
    return f"rss:{provider_name}:{ticker.strip().upper()}:{exchange_code.strip().upper()}"


def rss_batch_source_key(*, provider_name: str, provider_symbols: tuple[str, ...]) -> str:
    normalized = ",".join(symbol.strip().upper() for symbol in provider_symbols if symbol.strip())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"rss:{provider_name}:batch:{digest}"
