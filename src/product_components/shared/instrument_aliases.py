from __future__ import annotations

import re

_CLASS_DESIGNATION = re.compile(r"\s+class\s+[a-z]\b.*$", re.IGNORECASE)
_CORPORATE_SUFFIX = re.compile(
    r"[\s,]+(?:inc|corp|ltd|plc|llc|s\.a|n\.v|a\/s|co|company|corporation|incorporated|limited)\.?\s*$",
    re.IGNORECASE,
)
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_SHORT_NAME_TAIL_TOKENS = frozenset(
    {
        "devices",
        "energy",
        "holding",
        "holdings",
        "materials",
        "motor",
        "motors",
        "pharmaceutical",
        "pharmaceuticals",
        "semiconductor",
        "semiconductors",
        "solutions",
        "systems",
        "technologies",
        "technology",
        "therapeutic",
        "therapeutics",
    }
)
_CURATED_TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "BRK.A": ("berkshire",),
    "BRK.B": ("berkshire",),
    "GOOG": ("google",),
    "GOOGL": ("google",),
    "XOM": ("exxon",),
}


def base_company_name(display_name: str) -> str:
    """Return the plain company name without share-class and legal suffix noise."""
    name = _CLASS_DESIGNATION.sub("", display_name).strip()
    name = _CORPORATE_SUFFIX.sub("", name).strip()
    return name


def build_instrument_aliases(
    *,
    ticker: str,
    display_name: str | None,
    aliases: tuple[str, ...] = (),
) -> tuple[str, ...]:
    values = {
        *(alias for alias in aliases if alias and alias.strip()),
        ticker.strip(),
        *(required_instrument_aliases(ticker=ticker, display_name=display_name)),
    }
    if display_name and display_name.strip():
        values.add(display_name.strip())
    return _normalize_aliases(values)


def normalize_instrument_aliases(aliases: tuple[str, ...]) -> tuple[str, ...]:
    return _normalize_aliases(aliases)


def required_instrument_aliases(*, ticker: str, display_name: str | None) -> tuple[str, ...]:
    values: set[str] = set()
    if ticker.strip():
        values.update(_curated_aliases_for_ticker(ticker))
    if display_name and display_name.strip():
        base_name = base_company_name(display_name)
        values.update(_alias_variants(base_name))
        values.update(_alias_variants(_short_company_name(base_name)))
    return _normalize_aliases(values)


def missing_instrument_aliases(
    *,
    ticker: str,
    display_name: str | None,
    aliases: tuple[str, ...],
) -> tuple[str, ...]:
    stored = set(_normalize_aliases(aliases))
    required = set(required_instrument_aliases(ticker=ticker, display_name=display_name))
    return tuple(sorted(required - stored))


def has_missing_instrument_aliases(
    *,
    ticker: str,
    display_name: str | None,
    aliases: tuple[str, ...],
) -> bool:
    return bool(missing_instrument_aliases(ticker=ticker, display_name=display_name, aliases=aliases))


def _short_company_name(base_name: str) -> str:
    tokens = _alias_tokens(base_name)
    if len(tokens) < 2:
        return ""
    if tokens[-1] not in _SHORT_NAME_TAIL_TOKENS:
        return ""
    return " ".join(tokens[:-1]).strip()


def _curated_aliases_for_ticker(ticker: str) -> tuple[str, ...]:
    return _CURATED_TICKER_ALIASES.get(ticker.strip().upper(), ())


def _alias_variants(value: str) -> set[str]:
    normalized = value.strip()
    if not normalized:
        return set()
    variants = {normalized}
    tokenized = " ".join(_alias_tokens(normalized))
    if tokenized:
        variants.add(tokenized)
    return {item for item in variants if item}


def _alias_tokens(value: str) -> list[str]:
    return [token for token in _NON_ALPHANUMERIC.sub(" ", value.strip().lower()).split() if token]


def _normalize_aliases(values: set[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                value.strip().lower()
                for value in values
                if value and value.strip()
            }
        )
    )
