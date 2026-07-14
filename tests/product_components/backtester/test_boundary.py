from __future__ import annotations

import re
from pathlib import Path

# The Backtester is an offline consumer: it must read another component's data
# only through that component's documented export/read contract, never by
# querying the foreign schema directly (docs/design/product_components/backtester/
# behavior.md Section 9). This is the same guard market_data and thesis_builder
# already carry; without it a copied report CLI silently reached into the live
# thesis_builder schema and no test failed.
#
# We flag SQL query targets against a foreign component schema — `FROM <schema>.t_`
# or `JOIN <schema>.t_`. This deliberately does NOT flag:
#   * Python imports (`from src.product_components.thesis_builder... import ...`),
#     because `from` there is followed by `src`, not the schema name;
#   * the regeneration schema-template render in repository.py, which manipulates
#     the literal string `thesis_builder` but never as a `FROM thesis_builder.t_`
#     query target;
#   * reads of the Backtester's own `backtester` schema or the `sim_bt_<run_id>`
#     copies it creates and owns during regeneration.
_FOREIGN_SCHEMAS = ("thesis_builder", "news_fetcher", "market_data", "shared")
_FOREIGN_SQL = re.compile(
    r"\b(?:FROM|JOIN)\s+(" + "|".join(_FOREIGN_SCHEMAS) + r")\.t_",
    re.IGNORECASE,
)


def test_backtester_does_not_query_foreign_component_schemas() -> None:
    package = Path(__file__).resolve().parents[3] / "src" / "product_components" / "backtester"
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _FOREIGN_SQL.finditer(text):
            offenders.append(f"{path.relative_to(package)}: {match.group(0)!r}")
    assert offenders == [], (
        "Backtester must read foreign-component data through owning-component "
        f"contracts, not direct SQL: {offenders}"
    )
