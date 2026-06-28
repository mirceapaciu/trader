from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from src.product_components.thesis_builder.export import ExportedThesisCard

from .models import (
    BacktestRunParams,
    CardSnapshot,
    EquityPoint,
    RunMetrics,
    SimulatedTrade,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BacktesterRepository:
    def __init__(
        self,
        *,
        dsn: str,
        backtester_schema: str,
        market_data_schema: str,
        thesis_builder_schema: str,
        shared_schema: str,
    ) -> None:
        self._dsn = dsn
        self._backtester_schema = _safe_identifier(backtester_schema)
        self._market_data_schema = _safe_identifier(market_data_schema)
        self._thesis_builder_schema = _safe_identifier(thesis_builder_schema)
        self._shared_schema = _safe_identifier(shared_schema)

    def bootstrap_schema(self, *, repo_root: Path) -> None:
        components_root = repo_root / "src" / "product_components"
        schema_files = (
            components_root / "shared" / "db" / "schema.sql",
            components_root / "news_fetcher" / "db" / "schema.sql",
            components_root / "market_data" / "db" / "schema.sql",
            components_root / "thesis_builder" / "db" / "schema.sql",
            components_root / "backtester" / "db" / "schema.sql",
        )
        with closing(psycopg.connect(self._dsn)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                for schema_file in schema_files:
                    cursor.execute(schema_file.read_text(encoding="utf-8"))

    def create_run(
        self,
        *,
        params: BacktestRunParams,
        dataset_snapshot_hash: str,
    ) -> None:
        sql = (
            f"INSERT INTO {self._backtester_schema}.t_backtest_runs "
            f"(run_id, window_start_at, window_end_at, mode, timing_scenario, "
            f"ideal_fetch_delay_seconds, ideal_thesis_delay_seconds, dataset_snapshot_hash, "
            f"card_population, strategies_requested, initial_capital, "
            f"execution_model_snapshot_json, risk_model_snapshot_json, run_note, status) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running')"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    params.run_id,
                    _to_utc(params.window_start_at),
                    _to_utc(params.window_end_at),
                    params.mode.value,
                    params.timing_scenario.value,
                    params.ideal_fetch_delay_seconds,
                    params.ideal_thesis_delay_seconds,
                    dataset_snapshot_hash,
                    params.card_population.value,
                    params.strategies,
                    params.initial_capital,
                    Json(params.execution_model.snapshot()),
                    Json(params.risk_model.snapshot()),
                    params.run_note,
                ),
            )
            conn.commit()

    def insert_card_snapshots(
        self, *, run_id: str, snapshots: list[CardSnapshot]
    ) -> None:
        if not snapshots:
            return
        sql = (
            f"INSERT INTO {self._backtester_schema}.t_backtest_card_snapshots "
            f"(snapshot_id, run_id, thesis_card_id, ticker, exchange_code, direction, strategy, "
            f"time_horizon, confidence, decision_state, card_created_at, card_expires_at, "
            f"evidence_json, news_ready_at, risk_box_json, source_export_ref) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (run_id, thesis_card_id) DO NOTHING"
        )
        with self._connect() as conn, conn.cursor() as cur:
            for snapshot in snapshots:
                cur.execute(
                    sql,
                    (
                        snapshot.snapshot_id,
                        snapshot.run_id,
                        snapshot.thesis_card_id,
                        snapshot.ticker,
                        snapshot.exchange_code,
                        snapshot.direction,
                        snapshot.strategy,
                        snapshot.time_horizon,
                        snapshot.confidence,
                        snapshot.decision_state,
                        _to_utc(snapshot.card_created_at),
                        _to_utc(snapshot.card_expires_at),
                        Json(snapshot.evidence_json),
                        _to_utc(snapshot.news_ready_at),
                        Json(snapshot.risk_box_json),
                        snapshot.source_export_ref,
                    ),
                )
            conn.commit()

    def insert_trades(self, *, run_id: str, trades: list[SimulatedTrade]) -> None:
        if not trades:
            return
        sql = (
            f"INSERT INTO {self._backtester_schema}.t_backtest_trades "
            f"(trade_id, run_id, thesis_card_id, ticker, exchange_code, strategy, direction, "
            f"card_decision_state, card_was_live_expired, entry_timing_scenario, "
            f"news_published_at, news_fetched_at, card_created_at, news_fetch_delay_seconds, "
            f"thesis_build_delay_seconds, total_pipeline_delay_seconds, entry_at, entry_price, "
            f"quantity, exit_at, exit_price, gross_pnl, commission, slippage, net_pnl, "
            f"return_pct, exit_reason, risk_block_rule, holding_period_seconds) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            f"%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (run_id, thesis_card_id, entry_timing_scenario) DO NOTHING"
        )
        with self._connect() as conn, conn.cursor() as cur:
            for trade in trades:
                cur.execute(
                    sql,
                    (
                        trade.trade_id,
                        trade.run_id,
                        trade.thesis_card_id,
                        trade.ticker,
                        trade.exchange_code,
                        trade.strategy,
                        trade.direction,
                        trade.card_decision_state,
                        trade.card_was_live_expired,
                        trade.entry_timing_scenario,
                        _opt_utc(trade.news_published_at),
                        _opt_utc(trade.news_fetched_at),
                        _to_utc(trade.card_created_at),
                        trade.news_fetch_delay_seconds,
                        trade.thesis_build_delay_seconds,
                        trade.total_pipeline_delay_seconds,
                        _opt_utc(trade.entry_at),
                        trade.entry_price,
                        trade.quantity,
                        _opt_utc(trade.exit_at),
                        trade.exit_price,
                        trade.gross_pnl,
                        trade.commission,
                        trade.slippage,
                        trade.net_pnl,
                        trade.return_pct,
                        trade.exit_reason.value,
                        trade.risk_block_rule,
                        trade.holding_period_seconds,
                    ),
                )
            conn.commit()

    def insert_equity_points(self, *, run_id: str, points: list[EquityPoint]) -> None:
        if not points:
            return
        sql = (
            f"INSERT INTO {self._backtester_schema}.t_backtest_equity_points "
            f"(point_id, run_id, timing_scenario, as_of, equity, open_positions) "
            f"VALUES (%s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (point_id) DO NOTHING"
        )
        with self._connect() as conn, conn.cursor() as cur:
            for point in points:
                cur.execute(
                    sql,
                    (
                        point.point_id,
                        point.run_id,
                        point.timing_scenario,
                        _to_utc(point.as_of),
                        point.equity,
                        point.open_positions,
                    ),
                )
            conn.commit()

    def finalize_run_success(self, *, run_id: str, metrics: RunMetrics) -> None:
        sql = (
            f"UPDATE {self._backtester_schema}.t_backtest_runs SET "
            f"status = 'completed', finished_at = NOW(), error_code = NULL, "
            f"error_details_json = '{{}}'::jsonb, "
            f"cards_considered = %s, cards_in_population = %s, cards_live_executable = %s, "
            f"cards_skipped_no_price = %s, trades_opened = %s, trades_closed = %s, "
            f"trades_risk_blocked = %s, net_pnl = %s, gross_profit = %s, gross_loss = %s, "
            f"total_commission = %s, total_slippage = %s, total_return = %s, win_rate = %s, "
            f"avg_win = %s, avg_loss = %s, profit_factor = %s, expectancy = %s, "
            f"max_drawdown = %s, max_drawdown_duration_seconds = %s, sharpe_ratio = %s, "
            f"exposure_fraction = %s, signal_accuracy = %s, "
            f"avg_news_fetch_delay_seconds = %s, p95_news_fetch_delay_seconds = %s, "
            f"max_news_fetch_delay_seconds = %s, avg_thesis_build_delay_seconds = %s, "
            f"p95_thesis_build_delay_seconds = %s, max_thesis_build_delay_seconds = %s, "
            f"avg_total_pipeline_delay_seconds = %s, p95_total_pipeline_delay_seconds = %s, "
            f"max_total_pipeline_delay_seconds = %s, pnl_gap = %s, win_rate_gap = %s, "
            f"trades_flipped_by_delay = %s, summary_json = %s, summary_md = %s "
            f"WHERE run_id = %s"
        )
        params = (
            metrics.cards_considered,
            metrics.cards_in_population,
            metrics.cards_live_executable,
            metrics.cards_skipped_no_price,
            metrics.trades_opened,
            metrics.trades_closed,
            metrics.trades_risk_blocked,
            metrics.net_pnl,
            metrics.gross_profit,
            metrics.gross_loss,
            metrics.total_commission,
            metrics.total_slippage,
            metrics.total_return,
            metrics.win_rate,
            metrics.avg_win,
            metrics.avg_loss,
            metrics.profit_factor,
            metrics.expectancy,
            metrics.max_drawdown,
            metrics.max_drawdown_duration_seconds,
            metrics.sharpe_ratio,
            metrics.exposure_fraction,
            metrics.signal_accuracy,
            metrics.avg_news_fetch_delay_seconds,
            metrics.p95_news_fetch_delay_seconds,
            metrics.max_news_fetch_delay_seconds,
            metrics.avg_thesis_build_delay_seconds,
            metrics.p95_thesis_build_delay_seconds,
            metrics.max_thesis_build_delay_seconds,
            metrics.avg_total_pipeline_delay_seconds,
            metrics.p95_total_pipeline_delay_seconds,
            metrics.max_total_pipeline_delay_seconds,
            metrics.pnl_gap,
            metrics.win_rate_gap,
            metrics.trades_flipped_by_delay,
            Json(metrics.summary_json),
            metrics.summary_md,
            run_id,
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    def finalize_run_failure(
        self,
        *,
        run_id: str,
        error_code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        sql = (
            f"UPDATE {self._backtester_schema}.t_backtest_runs SET "
            f"status = 'failed', finished_at = NOW(), error_code = %s, error_details_json = %s "
            f"WHERE run_id = %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (error_code, Json(details or {}), run_id))
            conn.commit()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=False)


def backtester_schema_file(repo_root: Path) -> Path:
    return repo_root / "src" / "product_components" / "backtester" / "db" / "schema.sql"


def bootstrap_backtester_schema(*, dsn: str, repo_root: Path) -> None:
    """Create the backtester schema and its tables if they do not exist.

    Owned by the Backtester component so other components (e.g. the Monitoring
    UI) can ensure the schema exists by calling this function, instead of
    reaching into the Backtester's schema files or duplicating its DDL. The
    backtester ``schema.sql`` is self-contained (no cross-schema dependencies),
    so applying it alone is sufficient.
    """
    schema_sql = backtester_schema_file(repo_root).read_text(encoding="utf-8")
    with closing(psycopg.connect(dsn)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(schema_sql)


def dataset_snapshot_hash(cards: list[ExportedThesisCard]) -> str:
    payload = [
        {
            "id": card.id,
            "created_at": _to_utc(card.created_at).isoformat(),
        }
        for card in sorted(cards, key=lambda item: (_to_utc(item.created_at), item.id))
    ]
    normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _opt_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _to_utc(value)
