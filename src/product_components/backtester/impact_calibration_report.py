"""CLI: article-level impact calibration event study.

Loads analyses carrying a price_impact_magnitude through the ThesisBuilder-owned
analysis-export contract (``ThesisAnalysisHistoryExporter``; production
``thesis_builder`` schema or a regeneration ``sim_bt_<run_id>`` copy), computes
each analysis's realized direction-aligned move in ATR_20d units from cached
daily bars via the MarketData historical-bars API, and prints predicted-vs-
realized calibration tables. Both data sources are owning-component contracts, so
this CLI never queries another component's tables directly.

    uv run python -m src.product_components.backtester.impact_calibration_report \
        --analysis-schema thesis_builder --since 2026-07-14
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.product_components.market_data.factory import build_market_data_service
from src.product_components.market_data.settings import MarketDataSettings
from src.product_components.news_fetcher.env_loader import load_env_files
from src.product_components.thesis_builder.export import (
    ExportedAnalysis,
    ThesisAnalysisHistoryExporter,
)

from .impact_calibration import (
    DailyBar,
    ImpactObservation,
    atr_20d_from_bars,
    build_impact_calibration_report,
    compute_realized_moves,
    format_impact_calibration_markdown,
)
from .settings import BacktesterSettings

# Bars fetched around each instrument's article window: enough history before the
# earliest article for the ATR fallback, enough after the latest for the 5-session
# horizon.
_BARS_LOOKBACK_DAYS = 60
_BARS_LOOKAHEAD_DAYS = 10
# Window floor when --since is omitted; the exporter requires an explicit range.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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

    # Read through the ThesisBuilder-owned analysis-export contract rather than
    # querying t_news_analyses directly, honoring the component boundary. The
    # schema may be production thesis_builder or a regeneration sim_bt_<run_id>.
    exporter = ThesisAnalysisHistoryExporter(
        dsn=settings.postgres_dsn, thesis_schema=args.analysis_schema
    )
    # Rejected analyses are included by default on purpose: restricting the study
    # to analyses that survived the gates would reintroduce survivorship bias.
    analyses = exporter.export_analyses(
        window_start_at=args.since or _EPOCH,
        window_end_at=args.until or datetime.now(timezone.utc),
        event_type=args.event_type,
        price_impact_magnitude=args.magnitude,
        valid_only=args.valid_only,
    )
    analyses = [a for a in analyses if a.published_at is not None]
    if not analyses:
        print("No analyses with a price_impact_magnitude matched the filters.")
        return

    market_data_service, ibkr_gateway = build_market_data_service(
        MarketDataSettings.from_env(), with_providers=args.refresh_bars
    )
    try:
        observations = _build_observations(
            analyses,
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


def _build_observations(
    analyses: list[ExportedAnalysis],
    *,
    market_data_service,
    benchmark_ticker: str | None,
    benchmark_exchange_code: str,
) -> list[ImpactObservation]:
    by_instrument: dict[tuple[str, str], list[ExportedAnalysis]] = {}
    for analysis in analyses:
        key = (analysis.ticker, analysis.exchange_code)
        by_instrument.setdefault(key, []).append(analysis)

    observations: list[ImpactObservation] = []
    benchmark_cache: dict[tuple[datetime, datetime], list[DailyBar]] = {}
    for (ticker, exchange_code), instrument_rows in sorted(by_instrument.items()):
        published_times = [a.published_at for a in instrument_rows]
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

        for analysis in instrument_rows:
            published_at = analysis.published_at
            atr_20d = analysis.atr_20d
            if atr_20d is None or atr_20d <= 0:
                atr_20d = atr_20d_from_bars(bars, before=published_at)
            moves: dict[int, float | None] = {}
            if atr_20d is not None and atr_20d > 0:
                moves = compute_realized_moves(
                    bars,
                    published_at=published_at,
                    atr_20d=atr_20d,
                    direction=str(analysis.direction),
                    benchmark_bars=benchmark_bars,
                )
            observations.append(
                ImpactObservation(
                    analysis_id=analysis.analysis_id,
                    ticker=ticker,
                    exchange_code=exchange_code,
                    published_at=published_at,
                    direction=str(analysis.direction),
                    event_type=analysis.event_type,
                    magnitude=str(analysis.price_impact_magnitude),
                    impact_horizon=analysis.impact_horizon,
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    main()
