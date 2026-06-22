"""Shared text-matching helpers for instrument/alias matching.

Used by both the news_fetcher (RSS ticker matching) and the thesis_builder
(instrument resolution) so the two stages agree on what counts as a match.
"""

from __future__ import annotations

import re


def contains_term(text: str, term: str) -> bool:
    """Return True when ``term`` appears in ``text``.

    Single alphanumeric terms (e.g. tickers like ``mu`` or single-word aliases
    like ``micron``) are matched on word boundaries so that short aliases do not
    spuriously match inside ordinary words (e.g. ``mu`` must not match
    ``multi``). Multi-word or punctuated terms (e.g. ``micron technology``) fall
    back to substring matching.

    Matching is case-insensitive; callers do not need to lowercase beforehand.
    """
    normalized = term.strip().lower()
    if not normalized:
        return False
    haystack = text.lower()
    if re.fullmatch(r"[a-z0-9]+", normalized):
        return re.search(rf"\b{re.escape(normalized)}\b", haystack) is not None
    return normalized in haystack
