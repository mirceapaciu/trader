from __future__ import annotations

from pathlib import Path


def test_market_data_runtime_does_not_reference_shared_sql() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    runtime_files = [
        repo_root / "src" / "product_components" / "market_data" / "service.py",
        repo_root / "src" / "product_components" / "market_data" / "storage_adapter.py",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)

    assert "shared.t_watchlist_tickers" not in combined
    assert "shared.t_api_usage" not in combined
    assert ".t_watchlist_tickers" not in combined
    assert ".t_api_usage" not in combined
