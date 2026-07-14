from src.product_components.shared.instrument_aliases import (
    build_instrument_aliases,
    has_missing_instrument_aliases,
    missing_instrument_aliases,
)


def test_build_instrument_aliases_adds_press_name_and_spacing_variants() -> None:
    aliases = build_instrument_aliases(
        ticker="NVO",
        display_name="Novo-Nordisk A/S",
    )

    assert "nvo" in aliases
    assert "novo-nordisk" in aliases
    assert "novo nordisk" in aliases


def test_build_instrument_aliases_adds_curated_google_alias_for_alphabet() -> None:
    aliases = build_instrument_aliases(
        ticker="GOOGL",
        display_name="Alphabet Inc. Class A Common Stock",
    )

    assert "alphabet" in aliases
    assert "google" in aliases


def test_build_instrument_aliases_adds_short_company_names() -> None:
    aliases = build_instrument_aliases(
        ticker="MU",
        display_name="Micron Technology, Inc.",
    )

    assert "micron technology" in aliases
    assert "micron" in aliases


def test_missing_instrument_aliases_flags_curated_and_derived_gaps() -> None:
    missing = missing_instrument_aliases(
        ticker="GOOGL",
        display_name="Alphabet Inc. Class A Common Stock",
        aliases=("googl", "alphabet"),
    )

    assert missing == ("google",)


def test_display_name_and_ticker_cover_identity_aliases() -> None:
    assert missing_instrument_aliases(
        ticker="RHM",
        display_name="Rheinmetall AG",
        aliases=(),
    ) == ()


def test_has_missing_instrument_aliases_passes_when_press_name_set_is_complete() -> None:
    assert has_missing_instrument_aliases(
        ticker="XOM",
        display_name="Exxon Mobil Corporation",
        aliases=("xom", "exxon mobil", "exxon"),
    ) is False
