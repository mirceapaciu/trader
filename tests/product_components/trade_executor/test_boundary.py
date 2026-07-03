from __future__ import annotations

import re
from pathlib import Path

# Actual import of the package — not the substring in a comment/docstring.
_IB_IMPORT = re.compile(r"^\s*(?:import\s+ib_async|from\s+ib_async\b)", re.MULTILINE)


def test_ib_async_imported_only_in_the_gateway_module() -> None:
    """ib_async must be isolated to broker/ib_async_gateway.py.

    Keeping the broker dependency behind the BrokerGateway Protocol is what lets
    the pipeline and service be unit-tested with no network. This guards that seam.
    """
    package = Path(__file__).resolve().parents[3] / "src" / "product_components" / "trade_executor"
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        if path.name == "ib_async_gateway.py":
            continue
        text = path.read_text(encoding="utf-8")
        if _IB_IMPORT.search(text):
            offenders.append(str(path.relative_to(package)))
    assert offenders == [], f"ib_async imported outside the gateway module: {offenders}"
