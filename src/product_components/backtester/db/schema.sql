-- Backtester physical schema (PostgreSQL)

CREATE SCHEMA IF NOT EXISTS backtester;

CREATE TABLE IF NOT EXISTS backtester.t_backtest_runs (
    run_id TEXT PRIMARY KEY,
    window_start_at TIMESTAMPTZ NOT NULL,
    window_end_at TIMESTAMPTZ NOT NULL,
    mode TEXT NOT NULL,
    timing_scenario TEXT NOT NULL,
    ideal_fetch_delay_seconds INTEGER NOT NULL DEFAULT 0,
    ideal_thesis_delay_seconds INTEGER NOT NULL DEFAULT 0,
    dataset_snapshot_hash TEXT NOT NULL,
    card_population TEXT NOT NULL,
    strategies_requested TEXT[],
    initial_capital NUMERIC(18, 4) NOT NULL,
    execution_model_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_model_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    thesis_config_snapshot_json JSONB,
    run_note TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cards_considered INTEGER NOT NULL DEFAULT 0,
    cards_in_population INTEGER NOT NULL DEFAULT 0,
    cards_live_executable INTEGER NOT NULL DEFAULT 0,
    cards_skipped_no_price INTEGER NOT NULL DEFAULT 0,
    trades_opened INTEGER NOT NULL DEFAULT 0,
    trades_closed INTEGER NOT NULL DEFAULT 0,
    trades_risk_blocked INTEGER NOT NULL DEFAULT 0,
    net_pnl NUMERIC(18, 4) NOT NULL DEFAULT 0,
    gross_profit NUMERIC(18, 4) NOT NULL DEFAULT 0,
    gross_loss NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_commission NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_slippage NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_return NUMERIC(12, 5),
    win_rate NUMERIC(6, 5),
    avg_win NUMERIC(18, 4),
    avg_loss NUMERIC(18, 4),
    profit_factor NUMERIC(18, 5),
    expectancy NUMERIC(18, 4),
    max_drawdown NUMERIC(6, 5),
    max_drawdown_duration_seconds NUMERIC(18, 3),
    sharpe_ratio NUMERIC(18, 5),
    exposure_fraction NUMERIC(6, 5),
    signal_accuracy NUMERIC(6, 5),
    avg_news_fetch_delay_seconds NUMERIC(18, 3),
    p95_news_fetch_delay_seconds NUMERIC(18, 3),
    max_news_fetch_delay_seconds NUMERIC(18, 3),
    avg_thesis_build_delay_seconds NUMERIC(18, 3),
    p95_thesis_build_delay_seconds NUMERIC(18, 3),
    max_thesis_build_delay_seconds NUMERIC(18, 3),
    avg_total_pipeline_delay_seconds NUMERIC(18, 3),
    p95_total_pipeline_delay_seconds NUMERIC(18, 3),
    max_total_pipeline_delay_seconds NUMERIC(18, 3),
    pnl_gap NUMERIC(18, 4),
    win_rate_gap NUMERIC(6, 5),
    trades_flipped_by_delay INTEGER,
    llm_token_budget_limit INTEGER,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary_md TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT ck_backtest_runs_mode
        CHECK (mode IN ('replay', 'regeneration')),
    CONSTRAINT ck_backtest_runs_timing_scenario
        CHECK (timing_scenario IN ('ideal', 'actual', 'both')),
    CONSTRAINT ck_backtest_runs_card_population
        CHECK (card_population IN ('all', 'approved_only', 'rejected_only')),
    CONSTRAINT ck_backtest_runs_status
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_backtest_runs_window
        CHECK (window_start_at < window_end_at),
    CONSTRAINT ck_backtest_runs_snapshot_hash
        CHECK (dataset_snapshot_hash <> ''),
    CONSTRAINT ck_backtest_runs_ideal_delays_non_negative
        CHECK (ideal_fetch_delay_seconds >= 0 AND ideal_thesis_delay_seconds >= 0),
    CONSTRAINT ck_backtest_runs_counts_non_negative
        CHECK (
            cards_considered >= 0
            AND cards_in_population >= 0
            AND cards_live_executable >= 0
            AND cards_skipped_no_price >= 0
            AND trades_opened >= 0
            AND trades_closed >= 0
            AND trades_risk_blocked >= 0
        ),
    CONSTRAINT ck_backtest_runs_win_rate_bounds
        CHECK (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)),
    CONSTRAINT ck_backtest_runs_max_drawdown_bounds
        CHECK (max_drawdown IS NULL OR (max_drawdown >= 0 AND max_drawdown <= 1)),
    CONSTRAINT ck_backtest_runs_exposure_bounds
        CHECK (exposure_fraction IS NULL OR (exposure_fraction >= 0 AND exposure_fraction <= 1)),
    CONSTRAINT ck_backtest_runs_signal_accuracy_bounds
        CHECK (signal_accuracy IS NULL OR (signal_accuracy >= 0 AND signal_accuracy <= 1)),
    CONSTRAINT ck_backtest_runs_delays_non_negative
        CHECK (
            (avg_news_fetch_delay_seconds IS NULL OR avg_news_fetch_delay_seconds >= 0)
            AND (p95_news_fetch_delay_seconds IS NULL OR p95_news_fetch_delay_seconds >= 0)
            AND (max_news_fetch_delay_seconds IS NULL OR max_news_fetch_delay_seconds >= 0)
            AND (avg_thesis_build_delay_seconds IS NULL OR avg_thesis_build_delay_seconds >= 0)
            AND (p95_thesis_build_delay_seconds IS NULL OR p95_thesis_build_delay_seconds >= 0)
            AND (max_thesis_build_delay_seconds IS NULL OR max_thesis_build_delay_seconds >= 0)
            AND (avg_total_pipeline_delay_seconds IS NULL OR avg_total_pipeline_delay_seconds >= 0)
            AND (p95_total_pipeline_delay_seconds IS NULL OR p95_total_pipeline_delay_seconds >= 0)
            AND (max_total_pipeline_delay_seconds IS NULL OR max_total_pipeline_delay_seconds >= 0)
        ),
    CONSTRAINT ck_backtest_runs_win_rate_gap_bounds
        CHECK (win_rate_gap IS NULL OR (win_rate_gap >= -1 AND win_rate_gap <= 1)),
    CONSTRAINT ck_backtest_runs_flipped_non_negative
        CHECK (trades_flipped_by_delay IS NULL OR trades_flipped_by_delay >= 0),
    CONSTRAINT ck_backtest_runs_gap_requires_both
        CHECK (
            timing_scenario = 'both'
            OR (
                pnl_gap IS NULL
                AND win_rate_gap IS NULL
                AND trades_flipped_by_delay IS NULL
            )
        ),
    CONSTRAINT ck_backtest_runs_regeneration_requires_config
        CHECK (
            mode <> 'regeneration'
            OR (thesis_config_snapshot_json IS NOT NULL AND llm_token_budget_limit IS NOT NULL)
        ),
    CONSTRAINT ck_backtest_runs_replay_no_thesis_config
        CHECK (
            mode <> 'replay'
            OR (thesis_config_snapshot_json IS NULL AND llm_token_budget_limit IS NULL)
        ),
    CONSTRAINT ck_backtest_runs_finished_at_by_status
        CHECK (
            (status = 'running' AND finished_at IS NULL)
            OR (status IN ('completed', 'failed') AND finished_at IS NOT NULL)
        )
);

-- Regeneration mode records which LLM produced the cards so runs can be compared
-- across models. NULL for replay runs (which reuse existing production cards).
ALTER TABLE backtester.t_backtest_runs
    ADD COLUMN IF NOT EXISTS llm_model TEXT;

CREATE INDEX IF NOT EXISTS idx_backtest_runs_status_created
    ON backtester.t_backtest_runs (status, created_at DESC, run_id);

CREATE TABLE IF NOT EXISTS backtester.t_backtest_trades (
    trade_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thesis_card_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    card_decision_state TEXT NOT NULL,
    card_was_live_expired BOOLEAN NOT NULL DEFAULT FALSE,
    entry_timing_scenario TEXT NOT NULL,
    news_published_at TIMESTAMPTZ,
    news_fetched_at TIMESTAMPTZ,
    card_created_at TIMESTAMPTZ NOT NULL,
    news_fetch_delay_seconds NUMERIC(18, 3),
    thesis_build_delay_seconds NUMERIC(18, 3),
    total_pipeline_delay_seconds NUMERIC(18, 3),
    entry_at TIMESTAMPTZ,
    entry_price NUMERIC(18, 6),
    quantity NUMERIC(18, 6),
    exit_at TIMESTAMPTZ,
    exit_price NUMERIC(18, 6),
    gross_pnl NUMERIC(18, 4),
    commission NUMERIC(18, 4),
    slippage NUMERIC(18, 4),
    net_pnl NUMERIC(18, 4),
    return_pct NUMERIC(18, 6),
    exit_reason TEXT NOT NULL,
    risk_block_rule TEXT,
    holding_period_seconds NUMERIC(18, 3),
    mfe_pct NUMERIC(18, 6),
    mae_pct NUMERIC(18, 6),
    time_to_mfe_seconds NUMERIC(18, 3),
    time_to_mae_seconds NUMERIC(18, 3),
    horizon_returns_json JSONB,
    both_brackets_in_one_bar BOOLEAN,
    bar_coverage_ratio NUMERIC(8, 6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_backtest_trades_run_card_scenario
        UNIQUE (run_id, thesis_card_id, entry_timing_scenario),
    CONSTRAINT fk_backtest_trades_run
        FOREIGN KEY (run_id)
        REFERENCES backtester.t_backtest_runs (run_id)
        ON DELETE CASCADE,
    CONSTRAINT ck_backtest_trades_direction
        CHECK (direction IN ('buy', 'sell')),
    CONSTRAINT ck_backtest_trades_decision_state
        CHECK (card_decision_state IN ('approved', 'rejected')),
    CONSTRAINT ck_backtest_trades_entry_timing_scenario
        CHECK (entry_timing_scenario IN ('ideal', 'actual')),
    CONSTRAINT ck_backtest_trades_exit_reason
        CHECK (exit_reason IN (
            'take_profit',
            'stop_loss',
            'time_stop',
            'reversal',
            'window_end',
            'not_filled',
            'risk_blocked'
        )),
    CONSTRAINT ck_backtest_trades_delays_non_negative
        CHECK (
            (news_fetch_delay_seconds IS NULL OR news_fetch_delay_seconds >= 0)
            AND (thesis_build_delay_seconds IS NULL OR thesis_build_delay_seconds >= 0)
            AND (total_pipeline_delay_seconds IS NULL OR total_pipeline_delay_seconds >= 0)
        ),
    CONSTRAINT ck_backtest_trades_holding_non_negative
        CHECK (holding_period_seconds IS NULL OR holding_period_seconds >= 0),
    CONSTRAINT ck_backtest_trades_excursion_times_non_negative
        CHECK (
            (time_to_mfe_seconds IS NULL OR time_to_mfe_seconds >= 0)
            AND (time_to_mae_seconds IS NULL OR time_to_mae_seconds >= 0)
        ),
    CONSTRAINT ck_backtest_trades_bar_coverage_bounds
        CHECK (bar_coverage_ratio IS NULL OR (bar_coverage_ratio >= 0 AND bar_coverage_ratio <= 1)),
    CONSTRAINT ck_backtest_trades_risk_block_rule
        CHECK (
            (exit_reason = 'risk_blocked' AND risk_block_rule IS NOT NULL)
            OR (exit_reason <> 'risk_blocked' AND risk_block_rule IS NULL)
        ),
    CONSTRAINT ck_backtest_trades_closed_consistency
        CHECK (
            exit_reason IN ('not_filled', 'risk_blocked')
            OR (exit_at IS NOT NULL AND exit_price IS NOT NULL)
        )
);

ALTER TABLE backtester.t_backtest_trades
    ADD COLUMN IF NOT EXISTS mfe_pct NUMERIC(18, 6),
    ADD COLUMN IF NOT EXISTS mae_pct NUMERIC(18, 6),
    ADD COLUMN IF NOT EXISTS time_to_mfe_seconds NUMERIC(18, 3),
    ADD COLUMN IF NOT EXISTS time_to_mae_seconds NUMERIC(18, 3),
    ADD COLUMN IF NOT EXISTS horizon_returns_json JSONB,
    ADD COLUMN IF NOT EXISTS both_brackets_in_one_bar BOOLEAN,
    ADD COLUMN IF NOT EXISTS bar_coverage_ratio NUMERIC(8, 6);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_scenario_strategy
    ON backtester.t_backtest_trades (run_id, entry_timing_scenario, strategy, thesis_card_id);

CREATE TABLE IF NOT EXISTS backtester.t_backtest_equity_points (
    point_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    timing_scenario TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    equity NUMERIC(18, 4) NOT NULL,
    open_positions INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_backtest_equity_points_run
        FOREIGN KEY (run_id)
        REFERENCES backtester.t_backtest_runs (run_id)
        ON DELETE CASCADE,
    CONSTRAINT ck_backtest_equity_points_timing_scenario
        CHECK (timing_scenario IN ('ideal', 'actual')),
    CONSTRAINT ck_backtest_equity_points_open_positions_non_negative
        CHECK (open_positions >= 0)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_points_run_scenario_as_of
    ON backtester.t_backtest_equity_points (run_id, timing_scenario, as_of);

CREATE TABLE IF NOT EXISTS backtester.t_backtest_card_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thesis_card_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    time_horizon TEXT NOT NULL,
    confidence NUMERIC(6, 5) NOT NULL,
    decision_state TEXT NOT NULL,
    card_created_at TIMESTAMPTZ NOT NULL,
    card_expires_at TIMESTAMPTZ NOT NULL,
    evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    news_ready_at TIMESTAMPTZ NOT NULL,
    risk_box_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_export_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_backtest_card_snapshots_run_card
        UNIQUE (run_id, thesis_card_id),
    CONSTRAINT fk_backtest_card_snapshots_run
        FOREIGN KEY (run_id)
        REFERENCES backtester.t_backtest_runs (run_id)
        ON DELETE CASCADE,
    CONSTRAINT ck_backtest_card_snapshots_direction
        CHECK (direction IN ('buy', 'sell')),
    CONSTRAINT ck_backtest_card_snapshots_decision_state
        CHECK (decision_state IN ('approved', 'rejected')),
    CONSTRAINT ck_backtest_card_snapshots_confidence_bounds
        CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS idx_backtest_card_snapshots_run_card
    ON backtester.t_backtest_card_snapshots (run_id, thesis_card_id);
