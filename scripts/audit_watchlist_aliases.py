"""Audit and optionally backfill watchlist aliases in the shared registry.

Usage:
    uv run python -m scripts.audit_watchlist_aliases
    uv run python -m scripts.audit_watchlist_aliases --apply
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.shared.adapters import (
    PostgresSharedInstrumentAdmin,
    PostgresSharedInstrumentRegistry,
    SharedWatchlistEntryInput,
)
from src.product_components.thesis_builder.settings import ThesisBuilderSettings


def _load_env() -> None:
    root = Path(__file__).resolve().parent.parent
    load_env_files(
        root,
        filenames=(".env.shared", ".env.prod", ".env.monitoring-ui", ".env.thesis-builder", ".env.secrets"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report active watchlist rows with incomplete press-name aliases.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite active watchlist rows through the shared admin writer to backfill derived aliases.",
    )
    args = parser.parse_args()

    _load_env()
    settings = ThesisBuilderSettings.from_env()
    registry = PostgresSharedInstrumentRegistry(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
        watchlist_table="t_watchlist_tickers",
    )
    admin = PostgresSharedInstrumentAdmin(
        dsn=settings.postgres_dsn,
        shared_schema=settings.shared_db_schema,
    )

    rows = registry.list_watchlist_records(active_only=True)
    flagged = [row for row in rows if row.has_missing_aliases]
    if not flagged:
        print("watchlist alias audit: PASS (0 rows missing press-name aliases)")
        return

    print(f"watchlist alias audit: FAIL ({len(flagged)} rows missing press-name aliases)")
    for row in flagged:
        print(
            f"  {row.ticker}:{row.exchange_code} display_name={row.display_name!r} "
            f"missing={list(row.missing_aliases)!r}"
        )

    if not args.apply:
        return

    for row in flagged:
        admin.upsert_watchlist_entry(
            SharedWatchlistEntryInput(
                ticker=row.ticker,
                exchange_code=row.exchange_code,
                display_name=row.display_name or row.ticker,
                aliases=row.aliases,
                source=row.source,
            )
        )
    print(f"backfill applied to {len(flagged)} rows")


if __name__ == "__main__":
    main()
