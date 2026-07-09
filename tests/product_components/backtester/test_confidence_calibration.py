from __future__ import annotations

from src.product_components.backtester.confidence_calibration import (
    ConfidenceCalibrationTrade,
    build_confidence_calibration_report,
    closed_calibration_trades,
    format_confidence_calibration_markdown,
    parse_bucket_edges,
)


def _trade(
    confidence: float,
    net_pnl: float,
    *,
    return_pct: float | None = None,
    exit_reason: str = "take_profit",
    entry_timing_scenario: str = "ideal",
) -> ConfidenceCalibrationTrade:
    return ConfidenceCalibrationTrade(
        confidence=confidence,
        net_pnl=net_pnl,
        gross_pnl=net_pnl,
        return_pct=return_pct,
        exit_reason=exit_reason,
        entry_timing_scenario=entry_timing_scenario,
    )


def test_bucket_assignment_is_lower_inclusive_and_last_upper_inclusive() -> None:
    report = build_confidence_calibration_report(
        [
            _trade(0.0, 1.0),
            _trade(0.6, 2.0),
            _trade(0.7, -1.0),
            _trade(1.0, 4.0),
        ],
        bucket_edges=(0.0, 0.6, 0.7, 1.0),
        min_sample_size=1,
    )

    assert [bucket.trade_count for bucket in report.buckets] == [1, 1, 2]
    assert [bucket.label for bucket in report.buckets] == [
        "[0.00, 0.60)",
        "[0.60, 0.70)",
        "[0.70, 1.00]",
    ]


def test_closed_trade_filter_excludes_not_filled_and_risk_blocked() -> None:
    trades = [
        _trade(0.8, 10.0, exit_reason="take_profit"),
        _trade(0.8, 0.0, exit_reason="not_filled"),
        _trade(0.8, 0.0, exit_reason="risk_blocked"),
    ]

    assert closed_calibration_trades(trades) == [trades[0]]
    report = build_confidence_calibration_report(
        trades,
        bucket_edges=(0.0, 0.75, 1.0),
        min_sample_size=1,
    )
    assert report.trade_count == 1
    assert report.buckets[1].trade_count == 1


def test_empty_buckets_are_emitted_with_null_ratios() -> None:
    report = build_confidence_calibration_report(
        [_trade(0.82, 12.0)],
        bucket_edges=(0.0, 0.6, 0.8, 1.0),
        min_sample_size=1,
    )

    assert [bucket.trade_count for bucket in report.buckets] == [0, 0, 1]
    assert report.buckets[0].win_rate is None
    assert report.buckets[1].avg_net_pnl is None


def test_metrics_are_hand_computable_per_bucket() -> None:
    report = build_confidence_calibration_report(
        [
            _trade(0.81, 10.0, return_pct=0.10, exit_reason="take_profit"),
            _trade(0.82, -4.0, return_pct=-0.04, exit_reason="stop_loss"),
            _trade(0.83, 2.0, return_pct=0.02, exit_reason="time_stop"),
        ],
        bucket_edges=(0.0, 0.8, 1.0),
        min_sample_size=3,
    )

    bucket = report.buckets[1]
    assert bucket.trade_count == 3
    assert bucket.winning_trades == 2
    assert bucket.win_rate == 2 / 3
    assert bucket.avg_net_pnl == 8 / 3
    assert bucket.median_net_pnl == 2.0
    assert bucket.avg_return_pct == (0.10 - 0.04 + 0.02) / 3
    assert bucket.gross_profit == 12.0
    assert bucket.gross_loss == 4.0
    assert bucket.profit_factor == 3.0
    assert bucket.exit_reason_counts == {
        "stop_loss": 1,
        "take_profit": 1,
        "time_stop": 1,
    }


def test_scenario_filtering_can_be_applied_before_report_building() -> None:
    trades = [
        _trade(0.82, 10.0, entry_timing_scenario="ideal"),
        _trade(0.82, -10.0, entry_timing_scenario="actual"),
    ]
    ideal_only = [
        trade for trade in trades if trade.entry_timing_scenario == "ideal"
    ]

    report = build_confidence_calibration_report(
        ideal_only,
        bucket_edges=(0.0, 0.8, 1.0),
        min_sample_size=1,
    )

    assert report.trade_count == 1
    assert report.buckets[1].avg_net_pnl == 10.0


def test_small_samples_are_explicitly_flagged_in_json_and_markdown() -> None:
    report = build_confidence_calibration_report(
        [_trade(0.82, 10.0)],
        bucket_edges=(0.0, 0.8, 1.0),
        min_sample_size=30,
    )

    payload = report.to_dict()
    markdown = format_confidence_calibration_markdown(report)

    assert payload["sample_too_small"] is True
    assert payload["buckets"][1]["sample_too_small"] is True
    assert "Sample warning" in markdown
    assert "insufficient sample" in markdown


def test_parse_bucket_edges_defaults_and_validates() -> None:
    assert parse_bucket_edges("0,0.5,1") == (0.0, 0.5, 1.0)
    assert len(parse_bucket_edges(None)) > 2
