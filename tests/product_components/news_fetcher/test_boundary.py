from __future__ import annotations

from pathlib import Path


def test_news_fetcher_runtime_does_not_reference_shared_sql() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    runtime_files = [
        repo_root / "src" / "product_components" / "news_fetcher" / "config_sync.py",
        repo_root / "src" / "product_components" / "news_fetcher" / "service.py",
        repo_root / "src" / "product_components" / "news_fetcher" / "storage_adapter.py",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)

    assert "shared.t_watchlist_tickers" not in combined
    assert "shared.t_instrument_aliases" not in combined
    assert "shared.t_instruments" not in combined
    assert ".t_watchlist_tickers" not in combined
    assert ".t_instrument_aliases" not in combined
    assert ".t_instruments" not in combined
