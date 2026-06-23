"""Clean up incorrect (article -> instrument) assignments and remediate the
thesis cards built on them.

Before commit f8237c7 the ThesisBuilder tagged instruments to articles using a
loose substring match, which tagged instruments to articles that were not really
about them. Those analyses were persisted as ``validation_status='valid'`` and
accumulated into thesis cards. This script re-applies the current word-boundary
matching (``_resolve_instruments`` / ``contains_term``) to every stored analysis
and, for assignments that no longer match:

  * invalidates the analysis row (``rejection_reason_code='instrument_not_subject'``),
  * prunes the analysis + its article out of any thesis card that used it and
    invalidates that card (``rejection_reason_code='instrument_cleanup'``),
  * scrubs the analysis/article out of any evidence window that referenced it.

Detection is deterministic (no LLM calls). The script is dry-run by default.

Usage (run from the repo root):
    uv run python -m scripts.cleanup_instrument_assignments                  # dry-run, all data
    uv run python -m scripts.cleanup_instrument_assignments --ticker MU      # dry-run, one ticker
    uv run python -m scripts.cleanup_instrument_assignments --card-id <uuid> # dry-run, one card
    uv run python -m scripts.cleanup_instrument_assignments --execute        # apply changes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentRegistry,
    SharedInstrumentRecord,
)
from src.product_components.thesis_builder.repository import _article, _safe_identifier
from src.product_components.thesis_builder.service import _resolve_instruments
from src.product_components.thesis_builder.settings import ThesisBuilderSettings

ANALYSIS_REJECTION_CODE = "instrument_not_subject"
CARD_REJECTION_CODE = "instrument_cleanup"


# --------------------------------------------------------------------------- #
# Pure decision logic (unit-tested without a database)
# --------------------------------------------------------------------------- #
def analysis_is_incorrect(
    *,
    article_snapshot: dict[str, Any],
    ticker: str,
    active_instruments: list[SharedInstrumentRecord],
) -> bool:
    """Return True when ``ticker`` no longer resolves to the snapshot article.

    Reuses the production resolver so the cleanup matches the live pipeline.
    """
    if not article_snapshot:
        # Without a snapshot we cannot re-evaluate; leave the row untouched.
        return False
    article = _article(article_snapshot)
    matched_tickers = {
        match.ticker
        for match in _resolve_instruments(article=article, active_instruments=active_instruments)
    }
    return ticker not in matched_tickers


def prune_card(
    *,
    source_analysis_ids: list[int],
    evidence: list[dict[str, Any]],
    bad_analysis_ids: set[int],
    bad_article_ids: set[str],
) -> tuple[list[int], list[dict[str, Any]], bool]:
    """Return (pruned_source_analysis_ids, pruned_evidence, affected).

    ``affected`` is True when the card referenced any bad analysis id and should
    therefore be invalidated.
    """
    affected = any(analysis_id in bad_analysis_ids for analysis_id in source_analysis_ids)
    if not affected:
        return source_analysis_ids, evidence, False
    pruned_ids = [aid for aid in source_analysis_ids if aid not in bad_analysis_ids]
    pruned_evidence = [
        item for item in evidence if str(item.get("article_id") or "") not in bad_article_ids
    ]
    return pruned_ids, pruned_evidence, True


def prune_window(
    *,
    analysis_ids: list[int],
    bad_analysis_ids: set[int],
    analysis_article: dict[int, str],
) -> tuple[list[int], list[str], bool]:
    """Return (pruned_analysis_ids, recomputed_article_ids, affected).

    ``article_ids`` are recomputed from the surviving analyses so the window's
    article list never references an article that no longer has a surviving
    analysis in the window.
    """
    affected = any(analysis_id in bad_analysis_ids for analysis_id in analysis_ids)
    if not affected:
        return analysis_ids, [], False
    pruned_ids = [aid for aid in analysis_ids if aid not in bad_analysis_ids]
    article_ids: list[str] = []
    for aid in pruned_ids:
        article_id = analysis_article.get(aid)
        if article_id and article_id not in article_ids:
            article_ids.append(article_id)
    return pruned_ids, article_ids, True


# --------------------------------------------------------------------------- #
# Database scan + apply
# --------------------------------------------------------------------------- #
class InstrumentAssignmentCleanup:
    def __init__(self, *, dsn: str, thesis_schema: str) -> None:
        self._dsn = dsn
        self._schema = _safe_identifier(thesis_schema)

    def find_incorrect_analyses(
        self,
        *,
        active_instruments: list[SharedInstrumentRecord],
        ticker: str | None,
        card_id: str | None,
    ) -> list[dict[str, Any]]:
        clauses = ["validation_status = 'valid'"]
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = %s")
            params.append(ticker)
        if card_id:
            clauses.append(
                f"id IN (SELECT jsonb_array_elements_text(source_analysis_ids)::bigint "
                f"FROM {self._schema}.t_thesis_cards WHERE id = %s)"
            )
            params.append(card_id)
        sql = (
            f"SELECT id, article_id, ticker, article_snapshot "
            f"FROM {self._schema}.t_news_analyses "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY id"
        )
        incorrect: list[dict[str, Any]] = []
        with psycopg.connect(self._dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        for row in rows:
            snapshot = dict(row["article_snapshot"] or {})
            if analysis_is_incorrect(
                article_snapshot=snapshot,
                ticker=str(row["ticker"]),
                active_instruments=active_instruments,
            ):
                incorrect.append(
                    {
                        "id": int(row["id"]),
                        "article_id": str(row["article_id"]),
                        "ticker": str(row["ticker"]),
                        "headline": str(snapshot.get("headline") or ""),
                    }
                )
        return incorrect

    def find_affected_cards(self, *, bad_analysis_ids: set[int]) -> list[dict[str, Any]]:
        if not bad_analysis_ids:
            return []
        # source_analysis_ids is a JSONB array of integers; the `?|` operator only
        # matches string elements, so compare element-wise as bigints instead.
        sql = (
            f"SELECT id, ticker, source_analysis_ids, evidence, validation_status "
            f"FROM {self._schema}.t_thesis_cards "
            f"WHERE EXISTS ("
            f"SELECT 1 FROM jsonb_array_elements_text(source_analysis_ids) AS e(val) "
            f"WHERE e.val::bigint = ANY(%s))"
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (list(bad_analysis_ids),))
            rows = cur.fetchall()
        return [
            {
                "id": str(row["id"]),
                "ticker": str(row["ticker"]),
                "source_analysis_ids": [int(item) for item in (row["source_analysis_ids"] or [])],
                "evidence": list(row["evidence"] or []),
                "validation_status": str(row["validation_status"]),
            }
            for row in rows
        ]

    def find_affected_windows(self, *, bad_analysis_ids: set[int]) -> list[dict[str, Any]]:
        if not bad_analysis_ids:
            return []
        # analysis_ids is a JSONB array of integers (see find_affected_cards note).
        sql = (
            f"SELECT id, analysis_ids "
            f"FROM {self._schema}.t_evidence_windows "
            f"WHERE EXISTS ("
            f"SELECT 1 FROM jsonb_array_elements_text(analysis_ids) AS e(val) "
            f"WHERE e.val::bigint = ANY(%s))"
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (list(bad_analysis_ids),))
            rows = cur.fetchall()
        return [
            {
                "id": int(row["id"]),
                "analysis_ids": [int(item) for item in (row["analysis_ids"] or [])],
            }
            for row in rows
        ]

    def apply(
        self,
        *,
        bad_analysis_ids: set[int],
        cards: list[dict[str, Any]],
        windows: list[dict[str, Any]],
        analysis_article: dict[int, str],
    ) -> None:
        """Apply every change inside a single transaction."""
        bad_article_ids = {analysis_article[aid] for aid in bad_analysis_ids if aid in analysis_article}
        with psycopg.connect(self._dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._schema}.t_news_analyses "
                    f"SET validation_status = 'rejected', rejection_reason_code = %s, "
                    f"validation_errors = validation_errors || %s::jsonb "
                    f"WHERE id = ANY(%s)",
                    (ANALYSIS_REJECTION_CODE, Json([ANALYSIS_REJECTION_CODE]), list(bad_analysis_ids)),
                )
                for card in cards:
                    pruned_ids, pruned_evidence, _ = prune_card(
                        source_analysis_ids=card["source_analysis_ids"],
                        evidence=card["evidence"],
                        bad_analysis_ids=bad_analysis_ids,
                        bad_article_ids=bad_article_ids,
                    )
                    if card["validation_status"] == "valid":
                        # Invalidate the card but record the cleanup in validation_errors.
                        cur.execute(
                            f"UPDATE {self._schema}.t_thesis_cards "
                            f"SET source_analysis_ids = %s, evidence = %s, "
                            f"validation_status = 'rejected', rejection_reason_code = %s, "
                            f"validation_errors = validation_errors || %s::jsonb "
                            f"WHERE id = %s",
                            (
                                Json(pruned_ids),
                                Json(pruned_evidence),
                                CARD_REJECTION_CODE,
                                Json([CARD_REJECTION_CODE]),
                                card["id"],
                            ),
                        )
                    else:
                        # Already rejected: prune the bad article but keep the
                        # original rejection_reason_code for audit.
                        cur.execute(
                            f"UPDATE {self._schema}.t_thesis_cards "
                            f"SET source_analysis_ids = %s, evidence = %s, "
                            f"validation_errors = validation_errors || %s::jsonb "
                            f"WHERE id = %s",
                            (
                                Json(pruned_ids),
                                Json(pruned_evidence),
                                Json([CARD_REJECTION_CODE]),
                                card["id"],
                            ),
                        )
                for window in windows:
                    pruned_ids, article_ids, _ = prune_window(
                        analysis_ids=window["analysis_ids"],
                        bad_analysis_ids=bad_analysis_ids,
                        analysis_article=analysis_article,
                    )
                    cur.execute(
                        f"UPDATE {self._schema}.t_evidence_windows "
                        f"SET analysis_ids = %s, article_ids = %s, updated_at = NOW() "
                        f"WHERE id = %s",
                        (Json(pruned_ids), Json(article_ids), window["id"]),
                    )
            conn.commit()


def _load_env() -> None:
    root = Path(__file__).resolve().parent.parent
    load_env_files(
        root,
        filenames=(".env.shared", ".env.prod", ".env.thesis-builder", ".env.secrets"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Invalidate incorrect instrument assignments and the thesis cards built on them",
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--ticker", default=None, help="Restrict to a single ticker (e.g. MU)")
    parser.add_argument("--card-id", default=None, help="Restrict to analyses cited by one thesis card")
    args = parser.parse_args()

    # Article headlines contain non-cp1252 characters; keep printing robust on
    # Windows consoles instead of crashing on UnicodeEncodeError.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

    _load_env()
    settings = ThesisBuilderSettings.from_env()
    ticker = args.ticker.strip().upper() if args.ticker else None

    registry = PostgresSharedInstrumentRegistry(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
        watchlist_table="t_watchlist_tickers",
    )
    active_instruments = registry.list_active_instruments()

    cleanup = InstrumentAssignmentCleanup(
        dsn=settings.postgres_dsn,
        thesis_schema=settings.thesis_builder_db_schema,
    )

    incorrect = cleanup.find_incorrect_analyses(
        active_instruments=active_instruments, ticker=ticker, card_id=args.card_id
    )
    bad_analysis_ids = {item["id"] for item in incorrect}
    analysis_article = {item["id"]: item["article_id"] for item in incorrect}

    cards = cleanup.find_affected_cards(bad_analysis_ids=bad_analysis_ids)
    windows = cleanup.find_affected_windows(bad_analysis_ids=bad_analysis_ids)

    print(f"Active instruments: {len(active_instruments)}")
    print(f"Incorrect (article -> instrument) assignments: {len(incorrect)}")
    for item in incorrect:
        print(f"  analysis id={item['id']} ticker={item['ticker']} article={item['article_id']} :: {item['headline'][:90]}")
    valid_cards = sum(1 for card in cards if card["validation_status"] == "valid")
    print(f"Affected thesis cards: {len(cards)} ({valid_cards} currently valid -> will be invalidated)")
    for card in cards:
        removed = [
            str(ev.get("article_id"))
            for ev in card["evidence"]
            if str(ev.get("article_id") or "") in set(analysis_article.values())
        ]
        print(f"  card id={card['id']} ticker={card['ticker']} status={card['validation_status']} removing_articles={removed}")
    print(f"Affected evidence windows: {len(windows)}")

    if not args.execute:
        if bad_analysis_ids:
            print("\nDRY-RUN: re-run with --execute to apply.")
        else:
            print("\nNothing to clean up.")
        return

    if not bad_analysis_ids:
        print("\nNothing to clean up.")
        return

    cleanup.apply(
        bad_analysis_ids=bad_analysis_ids,
        cards=cards,
        windows=windows,
        analysis_article=analysis_article,
    )
    print(
        f"\nEXECUTED: analyses_invalidated={len(bad_analysis_ids)} "
        f"cards_invalidated={len(cards)} windows_scrubbed={len(windows)}"
    )


if __name__ == "__main__":
    main()
