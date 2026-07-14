"""CLI: article-level impact calibration event study.

Loads analyses carrying a price_impact_magnitude from a thesis schema (production
``thesis_builder`` or a regeneration ``sim_bt_<run_id>`` schema), computes each
analysis's realized direction-aligned move in ATR_20d units from cached daily
bars, and prints predicted-vs-realized calibration tables.

    uv run python -m src.product_components.backtester.impact_calibration_report \
        --analysis-schema thesis_builder --since 2026-07-14
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.product_components.market_data.factory import build_market_data_service
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.news_fetcher.env_loader import load_env_files

from .impact_calibration import (
    DailyBar,
    ImpactObservation,
    atr_20d_from_bars,
    build_impact_calibration_report,
    compute_realized_moves,
    format_impact_calibration_markdown,
)
from .settings import BacktesterSettings

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bars fetched around each instrument's article window: enough history before the
# earliest article for the ATR fallback, enough after the latest for the 5-session
# horizon.
_BARS_LOOKBACK_DAYS = 60
_BARS_LOOKAHEAD_DAYS = 10


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

    rows = _load_analyses(
        dsn=settings.postgres_dsn,
        schema=args.analysis_schema,
        since=args.since,
        until=args.until,
        event_type=args.event_type,
        magnitude=args.magnitude,
        valid_only=args.valid_only,
    )
    if not rows:
        print("No analyses with a price_impact_magnitude matched the filters.")
        return

    market_data_service, ibkr_gateway = build_market_data_service(
        MarketDataSettings.from_env(), with_providers=args.refresh_bars
    )
    try:
        observations = _build_observations(
            rows,
            market_data_service=market_data_service,
            benchmark_ticker=args.benchmark_ticker,
            benchmark_exchange_code=args.benchmark_exchange_code,
        )
    finally:
        if ibkr_gateway is not None:
            ibkr_gateway.disconnect()

    report = build_impact_calibration_report(
        observations, min_sample_size=args.min_sample_size
    )
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_impact_calibration_markdown(report))


def _load_analyses(
    *,
    dsn: str,
    schema: str,
    since: datetime | None,
    until: datetime | None,
    event_type: str | None,
    magnitude: str | None,
    valid_only: bool,
) -> list[dict[str, Any]]:
    safe_schema = _safe_identifier(schema)
    # Rejected analyses are included by default on purpose: restricting the study
    # to analyses that survived the gates would reintroduce survivorship bias.
    where = [
        "direction IN ('buy', 'sell')",
        "price_impact_magnitude IS NOT NULL",
        "article_snapshot ->> 'published_at' IS NOT NULL",
    ]
    params: list[Any] = []
    if since is not None:
        where.append("analyzed_at >= %s")
        params.append(since)
    if until is not None:
        where.append("analyzed_at < %s")
        params.append(until)
    if event_type:
        where.append("event_type = %s")
        params.append(event_type)
    if magnitude:
        where.append("price_impact_magnitude = %s")
        params.append(magnitude)
    if valid_only:
        where.append("validation_status = 'valid'")

    sql = (
        "SELECT id, ticker, exchange_code, direction, event_type, "
        "price_impact_magnitude, impact_horizon, "
        "article_snapshot ->> 'published_at' AS published_at, "
        "(market_context_snapshot ->> 'atr_20d')::float AS atr_20d "
        f"FROM {safe_schema}.t_news_analyses "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ticker, exchange_code, analyzed_at"
    )
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _build_observations(
    rows: list[dict[str, Any]],
    *,
    market_data_service,
    benchmark_ticker: str | None,
    benchmark_exchange_code: str,
) -> list[ImpactObservation]:
    by_instrument: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        published_at = _parse_published_at(row["published_at"])
        if published_at is None:
            continue
        row["_published_at"] = published_at
        key = (str(row["ticker"]), str(row["exchange_code"]))
        by_instrument.setdefault(key, []).append(row)

    observations: list[ImpactObservation] = []
    benchmark_cache: dict[tuple[datetime, datetime], list[DailyBar]] = {}
    for (ticker, exchange_code), instrument_rows in sorted(by_instrument.items()):
        published_times = [row["_published_at"] for row in instrument_rows]
        start = min(published_times) - timedelta(days=_BARS_LOOKBACK_DAYS)
        end = max(published_times) + timedelta(days=_BARS_LOOKAHEAD_DAYS)
        bars = _daily_bars(
            market_data_service, ticker=ticker, exchange_code=exchange_code, start=start, end=end
        )
        benchmark_bars = None
        if benchmark_ticker:
            window = (start, end)
            if window not in benchmark_cache:
                benchmark_cache[window] = _daily_bars(
                    market_data_service,
                    ticker=benchmark_ticker,
                    exchange_code=benchmark_exchange_code,
                    start=start,
                    end=end,
                )
            benchmark_bars = benchmark_cache[window]

        for row in instrument_rows:
            published_at = row["_published_at"]
            atr_20d = row["atr_20d"]
            if atr_20d is None or atr_20d <= 0:
                atr_20d = atr_20d_from_bars(bars, before=published_at)
            moves: dict[int, float | None] = {}
            if atr_20d is not None and atr_20d > 0:
                moves = compute_realized_moves(
                    bars,
                    published_at=published_at,
                    atr_20d=atr_20d,
                    direction=str(row["direction"]),
                    benchmark_bars=benchmark_bars,
                )
            observations.append(
                ImpactObservation(
                    analysis_id=int(row["id"]),
                    ticker=ticker,
                    exchange_code=exchange_code,
                    published_at=published_at,
                    direction=str(row["direction"]),
                    event_type=row["event_type"],
                    magnitude=str(row["price_impact_magnitude"]),
                    impact_horizon=row["impact_horizon"],
                    atr_20d=atr_20d,
                    moves_atr=moves,
                )
            )
    return observations


def _daily_bars(
    market_data_service, *, ticker: str, exchange_code: str, start: datetime, end: datetime
) -> list[DailyBar]:
    bars = market_data_service.get_historical_bars(
        ticker=ticker,
        exchange_code=exchange_code,
        interval="1d",
        start=start,
        end=end,
    )
    return [
        DailyBar(
            start_at=_to_utc(bar.bar_start_at),
            high=bar.high_price,
            low=bar.low_price,
            close=bar.close_price,
        )
        for bar in bars
    ]


def _parse_published_at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_utc(parsed)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backtester-impact-calibration")
    parser.add_argument(
        "--analysis-schema",
        default="thesis_builder",
        help="Schema holding t_news_analyses (production thesis_builder or a sim_bt_<run_id> schema).",
    )
    parser.add_argument("--since", type=_parse_cli_datetime)
    parser.add_argument("--until", type=_parse_cli_datetime)
    parser.add_argument("--event-type")
    parser.add_argument("--magnitude", choices=("low", "medium", "high"))
    parser.add_argument(
        "--valid-only",
        action="store_true",
        help="Restrict to validation_status='valid' analyses (default includes rejected ones to avoid survivorship bias).",
    )
    parser.add_argument("--min-sample-size", type=int, default=30)
    parser.add_argument(
        "--refresh-bars",
        action="store_true",
        help="Allow provider backfill of missing daily bars (default reads the cache only).",
    )
    parser.add_argument("--benchmark-ticker", help="Subtract this ticker's move (e.g. SPY) from each observation.")
    parser.add_argument("--benchmark-exchange-code", default="XNYS")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def _parse_cli_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _to_utc(parsed)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
