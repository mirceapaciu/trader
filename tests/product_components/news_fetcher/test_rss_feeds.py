from __future__ import annotations

from src.product_components.news_fetcher.rss_feeds import (
    build_rss_feed_url,
    rss_batch_source_key,
    rss_source_key,
)


def test_build_rss_feed_url_uses_default_yahoo_parameters() -> None:
    url = build_rss_feed_url(
        base_url="https://feeds.finance.yahoo.com/rss/2.0/headline",
        symbol_param="s",
        provider_symbol="NVDA",
        query_params={"region": "US", "lang": "en-US"},
    )

    assert url == "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US"


def test_build_rss_feed_url_uses_provider_symbol_override() -> None:
    url = build_rss_feed_url(
        base_url="https://feeds.finance.yahoo.com/rss/2.0/headline",
        symbol_param="s",
        provider_symbol="RHM.DE",
        query_params={"region": "DE", "lang": "en-US"},
    )

    assert url == "https://feeds.finance.yahoo.com/rss/2.0/headline?s=RHM.DE&region=DE&lang=en-US"


def test_build_rss_feed_url_urlencodes_special_symbols() -> None:
    url = build_rss_feed_url(
        base_url="https://feeds.finance.yahoo.com/rss/2.0/headline",
        symbol_param="s",
        provider_symbol="^GSPC",
        query_params={"region": "US", "lang": "en-US"},
    )

    assert url == "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US"


def test_build_rss_feed_url_supports_grouped_symbols() -> None:
    url = build_rss_feed_url(
        base_url="https://feeds.finance.yahoo.com/rss/2.0/headline",
        symbol_param="s",
        provider_symbol=("AXA", "MU", "NVDA", "RHM.DE"),
        query_params={"region": "US", "lang": "en-US"},
    )

    assert (
        url
        == "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AXA,MU,NVDA,RHM.DE&region=US&lang=en-US"
    )


def test_rss_source_key_uses_app_ticker_and_exchange() -> None:
    assert (
        rss_source_key(provider_name="yahoo_finance", ticker="sony", exchange_code="nyse")
        == "rss:yahoo_finance:SONY:NYSE"
    )


def test_rss_batch_source_key_is_stable() -> None:
    assert rss_batch_source_key(
        provider_name="yahoo_finance",
        provider_symbols=("AXA", "MU", "NVDA"),
    ) == rss_batch_source_key(
        provider_name="yahoo_finance",
        provider_symbols=("AXA", "MU", "NVDA"),
    )
