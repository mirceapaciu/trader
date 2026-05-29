# NewsFetcher News Sources

This document records the external news sources used or considered by NewsFetcher and the source-specific ingestion rules that affect a news-based trading workflow.

## Source Selection Principles

News sources are evaluated against these criteria:

- Ticker attribution: whether the source provides structured ticker/entity metadata.
- Latency: whether the source is useful for same-day or intraday trading signals.
- Reliability: whether the source has a documented API contract and predictable failure modes.
- Cost: whether the source fits the private-trader budget.
- Normalization burden: how much parsing, deduplication, and relevance filtering is required.
- Legal and operational risk: whether access is documented and intended for programmatic use.

For trading decisions, sources with structured ticker/entity metadata should be preferred over generic RSS feeds. RSS is useful as a zero-cost supplemental signal, but it should not be the only source for automated trade decisions.

## Finnhub

Role:
- Primary free-tier news provider.
- Provides company and market headlines.
- Used by default when `NEWS_SOURCE_FINNHUB_ENABLED=true`.

Strengths:
- Documented provider API.
- Free tier is usable for the current polling design.
- News payloads can include ticker metadata through provider fields.

Weaknesses:
- Free-tier quotas still need enforcement and monitoring.
- Provider relevance is not sufficient by itself; articles must still pass local watchlist and keyword filtering.

Operational guidance:
- Keep Finnhub as the default primary provider.
- Record each provider cycle in `news_fetcher.t_provider_cycle_status`.
- Treat quota, timeout, and schema failures as provider-level failures without stopping other providers.

Reference:
- https://finnhub.io/docs/api

## Marketaux

Role:
- Optional structured-news supplement.
- Used only when `NEWS_SOURCE_MARKETAUX_ENABLED=true` and `MARKETAUX_API_KEY` is configured.

Strengths:
- Provides structured entity metadata and provider sentiment fields.
- Useful as a fallback or enrichment source when Finnhub coverage is insufficient.

Weaknesses:
- Free tier has stricter daily request limits.
- Paid tier may be needed if the watchlist or polling frequency grows.

Operational guidance:
- Use it selectively for entity-tagged news and sentiment coverage.
- Keep `MARKETAUX_POLL_INTERVAL` separate from Finnhub and RSS polling.
- Monitor request counts and cost through shared API usage telemetry when available.

Reference:
- https://www.marketaux.com/documentation

## Yahoo Finance RSS

Role:
- Zero-cost supplemental RSS source for ticker-specific and broad market news.
- Used when `NEWS_SOURCE_RSS_ENABLED=true` and `news_fetcher.t_rss_sources` contains an enabled Yahoo Finance source.

Recommended URL pattern:

```text
https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>&region=US&lang=en-US
```

Example:

```text
https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US
```

Query parameters:

| Parameter | Purpose | Notes |
|-----------|---------|-------|
| `s` | Symbol query | Historically used for ticker-specific Yahoo Finance RSS feeds. Can contain one symbol, and historically also supported comma-separated symbols. |
| `region` | Region selection | Use `US` for the current app default unless a source-specific reason exists to use another region. |
| `lang` | Language selection | Use `en-US` for English news ingestion. |

Current confidence level:
- The US Yahoo Finance RSS endpoint is not backed by current public Yahoo documentation found during review.
- The `s=` symbol parameter is supported by historical Yahoo Finance RSS behavior and by third-party integrations that still document this URL shape.
- Yahoo Taiwan Finance currently documents RSS support for stock-code and keyword feeds, but that is a separate Yahoo Finance property and not a formal contract for the US endpoint.
- Treat Yahoo Finance RSS as an unofficial, best-effort source that can change or stop working without notice.

Ticker attribution rule:
- Do not rely only on parsing the ticker from the URL for long-term correctness.
- Store RSS feed configuration as DB metadata: configured ticker, exchange code, provider symbol, feed URL template, language, and region.
- When a feed is configured for one ticker, tag articles from that feed with the configured ticker even if the RSS item itself has no ticker field.
- For broad market feeds, leave tickers empty unless article text or later entity extraction maps the article to watchlist tickers.

Implemented configuration model:

```text
source_key=yahoo_finance
base_url=https://feeds.finance.yahoo.com/rss/2.0/headline
symbol_param=s
default_query_params={"region":"US","lang":"en-US"}
grouping_mode=grouped
max_symbols_per_request=10
min_request_interval_seconds=900

ticker=NVDA
exchange_code=NASDAQ
provider_symbol=NVDA
query_params={}
match_terms=["NVDA","NVIDIA"]
```

The runtime config uses `RSS_FEED_URLS` only for static or broad RSS feeds. Ticker-specific Yahoo Finance feeds are generated from `news_fetcher.t_rss_sources`, `news_fetcher.t_rss_symbol_rules`, and active `shared.t_watchlist_tickers` rows.

Per-ticker vs combined feeds:
- Prefer grouped Yahoo RSS requests for default ingestion because per-ticker requests hit Yahoo rate limits too easily.
- Use one `s=` parameter with comma-separated provider symbols, for example `s=AXA,MU,NVDA,RHM.DE`.
- Attribute grouped-feed articles by matching headline, summary, and URL against configured ticker match terms.
- Use single-symbol grouping mode only when debugging or when a source-specific reason requires it.

Server-side fetching:
- Fetch Yahoo RSS server-side through the NewsFetcher provider.
- Do not fetch Yahoo RSS directly from the browser UI; Yahoo RSS is not designed for browser-side CORS access.
- The NewsFetcher RSS provider should bypass inherited proxy environment variables when those variables are known to break local provider calls.

Expected limitations:
- Feeds are shallow and normally expose only recent items.
- RSS does not provide reliable history/backfill.
- RSS item metadata may omit ticker, source entity IDs, sentiment, and canonical provider IDs.
- The same article may appear across multiple feeds; dedupe by canonical URL remains required.
- Availability can vary by symbol, region, language, and Yahoo endpoint behavior.

Use in relevance filtering:
- Yahoo grouped-feed articles pass ticker relevance only when match terms identify a watchlist ticker in the article text.
- If no ticker match is found, the article must pass keyword matching or later entity extraction.
- Broad market Yahoo RSS items should not be assumed relevant to a watchlist ticker without matching text or explicit entity extraction.

References:
- Historical Yahoo Finance RSS note by former Yahoo engineer Jeremy Zawodny: https://jeremy.zawodny.com/blog/archives/003924.html
- TradingView charting docs that use the Yahoo Finance RSS URL pattern: https://charting-library-docs.xstaging.tv/latest/trading_terminal/news/
- Yahoo Taiwan Finance RSS service: https://tw.finance.yahoo.com/rss-index

## MarketWatch RSS

Role:
- Broad market supplemental RSS source.
- Useful for market context that can affect many tickers.

Strengths:
- Free RSS feeds are available for top stories and market pulse style updates.
- Useful for macro, market-structure, and broad risk-sentiment articles.

Weaknesses:
- Broad feeds usually do not carry structured ticker metadata.
- Many articles are market context rather than ticker-specific trading events.

Operational guidance:
- Use broad MarketWatch feeds only as supplemental context.
- Keep ticker attribution empty unless text/entity extraction maps the article to a watchlist ticker.
- Do not let broad market feeds bypass relevance filtering only because they are financial news.

Common feed examples:

```text
https://www.marketwatch.com/rss/topstories
https://www.marketwatch.com/rss/marketpulse
```

## Acceptance Rules by Source Type

Structured provider articles:
- Accept when provider tickers intersect `shared.t_watchlist_tickers`.
- Accept when configured include keywords match headline or summary.
- Reject when configured exclude keywords match.
- Deduplicate before persistence and publish.

Ticker-specific RSS articles:
- Accept when the feed is explicitly configured for a watchlist ticker.
- Accept when configured include keywords match headline or summary.
- Reject when configured exclude keywords match.
- Deduplicate across all feeds by canonical URL.

Broad RSS articles:
- Accept only when include keywords match or entity extraction maps the article to a watchlist ticker.
- Reject broad financial commentary that has no actionable market sentiment and no watchlist relevance.
- Deduplicate across all feeds by canonical URL.

## Recommended Source Priority

Default priority:

1. Finnhub for primary company and market headlines.
2. Marketaux for structured entity and sentiment supplementation when enabled.
3. Yahoo Finance RSS per watchlist ticker as a zero-cost supplemental source.
4. MarketWatch and other broad RSS feeds for market context.

RSS should be considered a recall-improving layer, not a replacement for structured provider APIs.
