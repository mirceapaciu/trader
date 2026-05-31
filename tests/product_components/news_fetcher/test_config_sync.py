from __future__ import annotations

import pytest

from src.product_components.news_fetcher.config_sync import (
    parse_instruments_config,
    parse_rss_sources_config,
)


def test_load_instruments_config_parses_reusable_aliases() -> None:
    config = parse_instruments_config(
        {
            "version": 1,
            "instruments": [
                {
                    "ticker": "RHM",
                    "exchange_code": "ETR",
                    "names": ["Rheinmetall"],
                    "aliases": ["RHM", "Rheinmetall"],
                    "identifiers": {"isin": "DE0007030009"},
                    "enabled": True,
                }
            ],
        }
    )

    assert len(config) == 1
    assert config[0].ticker == "RHM"
    assert config[0].aliases == ("RHM", "Rheinmetall")


def test_load_rss_sources_config_parses_static_and_dynamic_sources() -> None:
    sources = parse_rss_sources_config(
        {
            "version": 1,
            "sources": [
                {
                    "source_key": "rss:marketwatch:topstories",
                    "type": "static",
                    "url": "https://www.marketwatch.com/rss/topstories",
                    "enabled": True,
                    "min_request_interval_seconds": 900,
                },
                {
                    "source_key": "yahoo_finance",
                    "type": "dynamic_watchlist",
                    "base_url": "https://feeds.finance.yahoo.com/rss/2.0/headline",
                    "symbol_param": "s",
                    "default_query_params": {"region": "US", "lang": "en-US"},
                    "symbol_mappings": [
                        {
                            "ticker": "RHM",
                            "exchange_code": "ETR",
                            "provider_symbol": "RHM.DE",
                            "query_params": {"region": "DE", "lang": "en-US"},
                        }
                    ],
                },
            ],
        }
    )

    assert [source.source_key for source in sources] == [
        "rss:marketwatch:topstories",
        "yahoo_finance",
    ]
    assert sources[0].source_type == "static"
    assert sources[1].symbol_mappings[0].provider_symbol == "RHM.DE"


def test_load_rss_sources_config_rejects_duplicate_source_keys() -> None:
    with pytest.raises(ValueError, match="Duplicate source_key"):
        parse_rss_sources_config(
            {
                "version": 1,
                "sources": [
                    {
                        "source_key": "rss:marketwatch:topstories",
                        "type": "static",
                        "url": "https://www.marketwatch.com/rss/topstories",
                    },
                    {
                        "source_key": "rss:marketwatch:topstories",
                        "type": "static",
                        "url": "https://www.marketwatch.com/rss/topstories",
                    },
                ],
            }
        )
