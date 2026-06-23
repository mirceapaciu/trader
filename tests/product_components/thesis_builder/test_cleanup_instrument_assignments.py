from __future__ import annotations

from scripts.cleanup_instrument_assignments import (
    analysis_is_incorrect,
    prune_card,
    prune_window,
)
from src.product_components.shared.adapters import SharedInstrumentRecord


def _snapshot(*, headline: str = "", summary: str | None = None, url: str = "", tickers: list[str] | None = None) -> dict:
    return {
        "id": "art-1",
        "source": "rss",
        "headline": headline,
        "summary": summary,
        "url": url,
        "tickers": tickers or [],
        "published_at": "2026-06-20T12:00:00+00:00",
        "fetched_at": "2026-06-20T12:05:00+00:00",
        "sentiment_source": None,
    }


_MU = SharedInstrumentRecord(ticker="MU", exchange_code="XNAS", aliases=("micron",))
_MU_SHORT = SharedInstrumentRecord(ticker="MU", exchange_code="XNAS", aliases=("mu",))


def test_kept_when_ticker_listed_in_article_tickers() -> None:
    snapshot = _snapshot(headline="Markets roundup", tickers=["MU", "AAPL"])
    assert analysis_is_incorrect(article_snapshot=snapshot, ticker="MU", active_instruments=[_MU]) is False


def test_kept_when_alias_matches_on_word_boundary() -> None:
    snapshot = _snapshot(headline="Micron Technology shares jump on earnings", tickers=[])
    assert analysis_is_incorrect(article_snapshot=snapshot, ticker="MU", active_instruments=[_MU]) is False


def test_flagged_when_alias_is_only_a_substring_false_positive() -> None:
    snapshot = _snapshot(headline="Fiscal stimulus boosts broad market", tickers=[])
    assert analysis_is_incorrect(article_snapshot=snapshot, ticker="MU", active_instruments=[_MU_SHORT]) is True


def test_flagged_when_instrument_absent_entirely() -> None:
    snapshot = _snapshot(headline="Oil prices climb on supply concerns", tickers=["XOM"])
    assert analysis_is_incorrect(article_snapshot=snapshot, ticker="MU", active_instruments=[_MU]) is True


def test_empty_snapshot_is_left_untouched() -> None:
    assert analysis_is_incorrect(article_snapshot={}, ticker="MU", active_instruments=[_MU]) is False


def test_prune_card_removes_bad_analysis_and_evidence() -> None:
    evidence = [
        {"article_id": "a", "bullet": "x"},
        {"article_id": "b", "bullet": "y"},
        {"article_id": "c", "bullet": "z"},
    ]
    pruned_ids, pruned_evidence, affected = prune_card(
        source_analysis_ids=[1, 2, 3],
        evidence=evidence,
        bad_analysis_ids={2},
        bad_article_ids={"b"},
    )
    assert affected is True
    assert pruned_ids == [1, 3]
    assert [item["article_id"] for item in pruned_evidence] == ["a", "c"]


def test_prune_card_no_op_when_unaffected() -> None:
    evidence = [{"article_id": "a", "bullet": "x"}]
    pruned_ids, pruned_evidence, affected = prune_card(
        source_analysis_ids=[1],
        evidence=evidence,
        bad_analysis_ids={99},
        bad_article_ids={"zzz"},
    )
    assert affected is False
    assert pruned_ids == [1]
    assert pruned_evidence == evidence


def test_prune_window_recomputes_article_ids_from_survivors() -> None:
    pruned_ids, article_ids, affected = prune_window(
        analysis_ids=[1, 2, 3],
        bad_analysis_ids={2},
        analysis_article={1: "a", 2: "b", 3: "a"},
    )
    assert affected is True
    assert pruned_ids == [1, 3]
    # article "a" still has surviving analyses 1 and 3; "b" is gone.
    assert article_ids == ["a"]


def test_prune_window_no_op_when_unaffected() -> None:
    pruned_ids, article_ids, affected = prune_window(
        analysis_ids=[1, 3],
        bad_analysis_ids={2},
        analysis_article={1: "a", 3: "a"},
    )
    assert affected is False
    assert pruned_ids == [1, 3]
    assert article_ids == []
