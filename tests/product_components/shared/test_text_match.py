import pytest

from src.product_components.shared.text_match import contains_term


@pytest.mark.parametrize(
    "text,term,expected",
    [
        # Regression: 2-letter ticker alias must not match inside ordinary words.
        ("Exploring the top bargains in a multi-trillion-dollar industry.", "mu", False),
        ("MU rallies today on strong demand", "mu", True),
        ("Micron Technology beats estimates", "mu", False),
        # Single-word aliases match on word boundaries (case-insensitive).
        ("Micron Technology beats estimates", "micron", True),
        ("micromanaging the supply chain", "micron", False),
        # Multi-word / punctuated aliases fall back to substring matching.
        ("Shares of Micron Technology, Inc. jumped", "micron technology", True),
        ("No memory names here", "micron technology", False),
        # Empty / whitespace terms never match.
        ("anything at all", "", False),
        ("anything at all", "   ", False),
    ],
)
def test_contains_term(text: str, term: str, expected: bool) -> None:
    assert contains_term(text, term) is expected
