from __future__ import annotations

import re
from pathlib import Path

# Actual import of the package — not the substring in a comment/docstring.
_IB_IMPORT = re.compile(r"^\s*(?:import\s+ib_async|from\s+ib_async\b)", re.MULTILINE)


def test_ib_async_imported_only_in_the_gateway_module() -> None:
    """ib_async must be isolated to market_data/ibkr_gateway.py.

    Keeping the broker dependency behind the injected gateway is what lets the service and
    providers be unit-tested with no network. This guards that seam.
    """
    package = Path(__file__).resolve().parents[3] / "src" / "product_components" / "market_data"
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        if path.name == "ibkr_gateway.py":
            continue
        text = path.read_text(encoding="utf-8")
        if _IB_IMPORT.search(text):
            offenders.append(str(path.relative_to(package)))
    assert offenders == [], f"ib_async imported outside the gateway module: {offenders}"


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
