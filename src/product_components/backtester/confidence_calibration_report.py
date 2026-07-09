from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.product_components.news_fetcher.env_loader import load_env_files

from .confidence_calibration import (
    ConfidenceCalibrationTrade,
    build_confidence_calibration_report,
    format_confidence_calibration_markdown,
    parse_bucket_edges,
)
from .settings import BacktesterSettings

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> None:
    repo_root = _repo_root()
    load_env_files(
        repo_root,
        filenames=(
            ".env.shared",
            ".env.prod",
            ".env.backtester",
            ".env.secrets",
        ),
        override_existing=False,
    )
    args = _parse_args()
    settings = BacktesterSettings.from_env()
    bucket_edges = parse_bucket_edges(args.bucket_edges)
    trades = _load_trades(
        dsn=settings.postgres_dsn,
        schema=settings.db_schema,
        run_id=args.run_id,
        entry_timing_scenario=args.entry_timing_scenario,
        strategy=args.strategy,
        direction=args.direction,
    )
    report = build_confidence_calibration_report(
        trades,
        bucket_edges=bucket_edges,
        min_sample_size=args.min_sample_size,
    )
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_confidence_calibration_markdown(report))


def _load_trades(
    *,
    dsn: str,
    schema: str,
    run_id: str | None,
    entry_timing_scenario: str | None,
    strategy: str | None,
    direction: str | None,
) -> list[ConfidenceCalibrationTrade]:
    safe_schema = _safe_identifier(schema)
    where = ["r.status = 'completed'", "t.exit_reason NOT IN ('not_filled', 'risk_blocked')"]
    params: list[Any] = []
    if run_id:
        where.append("t.run_id = %s")
        params.append(run_id)
    if entry_timing_scenario:
        where.append("t.entry_timing_scenario = %s")
        params.append(entry_timing_scenario)
    if strategy:
        where.append("t.strategy = %s")
        params.append(strategy)
    if direction:
        where.append("t.direction = %s")
        params.append(direction)

    sql = (
        "SELECT t.run_id, t.thesis_card_id, s.confidence, t.net_pnl, t.gross_pnl, "
        "t.return_pct, t.exit_reason, t.entry_timing_scenario, t.strategy, t.direction "
        f"FROM {safe_schema}.t_backtest_trades t "
        f"JOIN {safe_schema}.t_backtest_card_snapshots s "
        "ON s.run_id = t.run_id AND s.thesis_card_id = t.thesis_card_id "
        f"JOIN {safe_schema}.t_backtest_runs r ON r.run_id = t.run_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY t.run_id, t.entry_timing_scenario, t.thesis_card_id"
    )
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        ConfidenceCalibrationTrade(
            run_id=row["run_id"],
            thesis_card_id=row["thesis_card_id"],
            confidence=float(row["confidence"]),
            net_pnl=float(row["net_pnl"]),
            gross_pnl=_float_or_none(row["gross_pnl"]),
            return_pct=_float_or_none(row["return_pct"]),
            exit_reason=row["exit_reason"],
            entry_timing_scenario=row["entry_timing_scenario"],
            strategy=row["strategy"],
            direction=row["direction"],
        )
        for row in rows
        if row["net_pnl"] is not None
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backtester-confidence-calibration")
    parser.add_argument("--run-id")
    parser.add_argument("--entry-timing-scenario", choices=("ideal", "actual"))
    parser.add_argument("--strategy")
    parser.add_argument("--direction", choices=("buy", "sell"))
    parser.add_argument(
        "--bucket-edges",
        help="Comma-separated confidence bucket edges. Defaults to 0,0.6,0.7,0.75,0.8,0.85,0.9,1.",
    )
    parser.add_argument("--min-sample-size", type=int, default=30)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
