from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import Any

NON_CLOSED_EXIT_REASONS = frozenset({"not_filled", "risk_blocked"})
DEFAULT_BUCKET_EDGES = (0.0, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0)
DEFAULT_MIN_SAMPLE_SIZE = 30


@dataclass(frozen=True)
class ConfidenceCalibrationTrade:
    confidence: float
    net_pnl: float
    gross_pnl: float | None = None
    return_pct: float | None = None
    exit_reason: str = ""
    entry_timing_scenario: str | None = None
    run_id: str | None = None
    thesis_card_id: str | None = None
    strategy: str | None = None
    direction: str | None = None


@dataclass(frozen=True)
class ConfidenceBucketMetrics:
    lower: float
    upper: float
    is_last: bool
    trade_count: int
    winning_trades: int
    win_rate: float | None
    avg_net_pnl: float | None
    median_net_pnl: float | None
    avg_return_pct: float | None
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    exit_reason_counts: dict[str, int]
    sample_too_small: bool

    @property
    def label(self) -> str:
        suffix = "]" if self.is_last else ")"
        return f"[{self.lower:.2f}, {self.upper:.2f}{suffix}"


@dataclass(frozen=True)
class ConfidenceCalibrationReport:
    buckets: tuple[ConfidenceBucketMetrics, ...]
    trade_count: int
    bucket_edges: tuple[float, ...]
    min_sample_size: int
    sample_too_small: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "bucket_edges": list(self.bucket_edges),
            "min_sample_size": self.min_sample_size,
            "sample_too_small": self.sample_too_small,
            "buckets": [
                {
                    "label": bucket.label,
                    "lower": bucket.lower,
                    "upper": bucket.upper,
                    "trade_count": bucket.trade_count,
                    "winning_trades": bucket.winning_trades,
                    "win_rate": bucket.win_rate,
                    "avg_net_pnl": bucket.avg_net_pnl,
                    "median_net_pnl": bucket.median_net_pnl,
                    "avg_return_pct": bucket.avg_return_pct,
                    "gross_profit": bucket.gross_profit,
                    "gross_loss": bucket.gross_loss,
                    "profit_factor": bucket.profit_factor,
                    "exit_reason_counts": bucket.exit_reason_counts,
                    "sample_too_small": bucket.sample_too_small,
                }
                for bucket in self.buckets
            ],
        }


def closed_calibration_trades(
    trades: list[ConfidenceCalibrationTrade],
) -> list[ConfidenceCalibrationTrade]:
    return [
        trade
        for trade in trades
        if trade.exit_reason not in NON_CLOSED_EXIT_REASONS
    ]


def build_confidence_calibration_report(
    trades: list[ConfidenceCalibrationTrade],
    *,
    bucket_edges: tuple[float, ...] = DEFAULT_BUCKET_EDGES,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> ConfidenceCalibrationReport:
    _validate_bucket_edges(bucket_edges)
    if min_sample_size < 1:
        raise ValueError("min_sample_size must be positive")

    closed_trades = closed_calibration_trades(trades)
    bucketed = {index: [] for index in range(len(bucket_edges) - 1)}
    for trade in closed_trades:
        bucketed[_bucket_index(trade.confidence, bucket_edges)].append(trade)

    buckets = tuple(
        _bucket_metrics(
            bucketed[index],
            lower=bucket_edges[index],
            upper=bucket_edges[index + 1],
            is_last=index == len(bucket_edges) - 2,
            min_sample_size=min_sample_size,
        )
        for index in range(len(bucket_edges) - 1)
    )
    return ConfidenceCalibrationReport(
        buckets=buckets,
        trade_count=len(closed_trades),
        bucket_edges=bucket_edges,
        min_sample_size=min_sample_size,
        sample_too_small=len(closed_trades) < min_sample_size,
    )


def format_confidence_calibration_markdown(
    report: ConfidenceCalibrationReport,
) -> str:
    lines = [
        "# Confidence Calibration Report",
        "",
        f"Closed trades: {report.trade_count}",
        f"Minimum sample size for calibration: {report.min_sample_size}",
    ]
    if report.sample_too_small:
        lines.append(
            "Sample warning: total closed trades are below the calibration threshold; "
            "treat bucket metrics as descriptive only."
        )
    lines.extend(
        [
            "",
            "| Confidence bucket | Trades | Win rate | Avg net P&L | Median net P&L | Avg return | Profit factor | Sample warning |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for bucket in report.buckets:
        lines.append(
            "| "
            + " | ".join(
                [
                    bucket.label,
                    str(bucket.trade_count),
                    _fmt_pct(bucket.win_rate),
                    _fmt_money(bucket.avg_net_pnl),
                    _fmt_money(bucket.median_net_pnl),
                    _fmt_pct(bucket.avg_return_pct),
                    _fmt_float(bucket.profit_factor),
                    "insufficient sample" if bucket.sample_too_small else "",
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "Empty buckets are shown intentionally so operators can see whether confidence "
        "values cluster inside an always-passing band."
    )
    return "\n".join(lines)


def parse_bucket_edges(value: str | None) -> tuple[float, ...]:
    if value is None or not value.strip():
        return DEFAULT_BUCKET_EDGES
    edges = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    _validate_bucket_edges(edges)
    return edges


def _bucket_index(confidence: float, edges: tuple[float, ...]) -> int:
    if confidence < edges[0] or confidence > edges[-1]:
        raise ValueError(f"confidence outside bucket range: {confidence}")
    for index, lower in enumerate(edges[:-1]):
        upper = edges[index + 1]
        is_last = index == len(edges) - 2
        if lower <= confidence < upper or (is_last and confidence == upper):
            return index
    raise ValueError(f"confidence outside bucket range: {confidence}")


def _bucket_metrics(
    trades: list[ConfidenceCalibrationTrade],
    *,
    lower: float,
    upper: float,
    is_last: bool,
    min_sample_size: int,
) -> ConfidenceBucketMetrics:
    trade_count = len(trades)
    net_pnls = [trade.net_pnl for trade in trades]
    return_pcts = [trade.return_pct for trade in trades if trade.return_pct is not None]
    winning_trades = sum(1 for trade in trades if trade.net_pnl > 0)
    gross_profit = sum(value for value in net_pnls if value > 0)
    gross_loss = abs(sum(value for value in net_pnls if value < 0))
    return ConfidenceBucketMetrics(
        lower=lower,
        upper=upper,
        is_last=is_last,
        trade_count=trade_count,
        winning_trades=winning_trades,
        win_rate=_ratio(winning_trades, trade_count),
        avg_net_pnl=_average(net_pnls),
        median_net_pnl=median(net_pnls) if net_pnls else None,
        avg_return_pct=_average(return_pcts),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        exit_reason_counts=dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        sample_too_small=trade_count < min_sample_size,
    )


def _validate_bucket_edges(edges: tuple[float, ...]) -> None:
    if len(edges) < 2:
        raise ValueError("at least two bucket edges are required")
    if edges[0] < 0 or edges[-1] > 1:
        raise ValueError("bucket edges must stay within [0, 1]")
    if any(left >= right for left, right in zip(edges, edges[1:])):
        raise ValueError("bucket edges must be strictly increasing")


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _fmt_money(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1%}"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
