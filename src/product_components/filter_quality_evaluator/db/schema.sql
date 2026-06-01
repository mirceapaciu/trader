-- Filter Quality Evaluator physical schema (PostgreSQL)

CREATE SCHEMA IF NOT EXISTS filter_quality_evaluator;

CREATE TABLE IF NOT EXISTS filter_quality_evaluator.t_filter_quality_runs (
    run_id TEXT PRIMARY KEY,
    news_window_start_at TIMESTAMPTZ NOT NULL,
    news_window_end_at TIMESTAMPTZ NOT NULL,
    dataset_snapshot_hash TEXT NOT NULL,
    filter_config_fingerprint TEXT,
    trigger_mode TEXT NOT NULL DEFAULT 'manual_cli',
    run_note TEXT,
    accepted_audit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    accepted_audit_sample_size INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    error_details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    dataset_input_count INTEGER NOT NULL DEFAULT 0,
    dataset_rejected_count INTEGER NOT NULL DEFAULT 0,
    dataset_accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_items_evaluated INTEGER NOT NULL DEFAULT 0,
    accepted_items_sampled INTEGER NOT NULL DEFAULT 0,
    correctly_rejected_count INTEGER NOT NULL DEFAULT 0,
    incorrectly_rejected_count INTEGER NOT NULL DEFAULT 0,
    correctly_accepted_count INTEGER NOT NULL DEFAULT 0,
    incorrectly_accepted_count INTEGER NOT NULL DEFAULT 0,
    rejection_precision_proxy NUMERIC(6, 5),
    incorrectly_accepted_rate_estimate NUMERIC(6, 5),
    token_budget_limit INTEGER NOT NULL,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommendation_summary_md TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT ck_filter_quality_runs_trigger_mode
        CHECK (trigger_mode IN ('manual_cli')),
    CONSTRAINT ck_filter_quality_runs_status
        CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_filter_quality_runs_window
        CHECK (news_window_start_at < news_window_end_at),
    CONSTRAINT ck_filter_quality_runs_snapshot_hash
        CHECK (dataset_snapshot_hash <> ''),
    CONSTRAINT ck_filter_quality_runs_audit_sample_positive
        CHECK (accepted_audit_sample_size IS NULL OR accepted_audit_sample_size > 0),
    CONSTRAINT ck_filter_quality_runs_audit_sample_guard
        CHECK (accepted_audit_enabled = TRUE OR accepted_audit_sample_size IS NULL),
    CONSTRAINT ck_filter_quality_runs_counts_non_negative
        CHECK (
            dataset_input_count >= 0
            AND dataset_rejected_count >= 0
            AND dataset_accepted_count >= 0
            AND rejected_items_evaluated >= 0
            AND accepted_items_sampled >= 0
            AND correctly_rejected_count >= 0
            AND incorrectly_rejected_count >= 0
            AND correctly_accepted_count >= 0
            AND incorrectly_accepted_count >= 0
        ),
    CONSTRAINT ck_filter_quality_runs_budget_limit
        CHECK (token_budget_limit > 0),
    CONSTRAINT ck_filter_quality_runs_rejection_precision_proxy
        CHECK (
            rejection_precision_proxy IS NULL
            OR (
                rejection_precision_proxy >= 0
                AND rejection_precision_proxy <= 1
            )
        ),
    CONSTRAINT ck_filter_quality_runs_incorrectly_accepted_rate_estimate
        CHECK (
            incorrectly_accepted_rate_estimate IS NULL
            OR (
                incorrectly_accepted_rate_estimate >= 0
                AND incorrectly_accepted_rate_estimate <= 1
            )
        ),
    CONSTRAINT ck_filter_quality_runs_finished_at_by_status
        CHECK (
            (status = 'running' AND finished_at IS NULL)
            OR (status IN ('completed', 'failed') AND finished_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_filter_quality_runs_status_created
    ON filter_quality_evaluator.t_filter_quality_runs (status, created_at DESC, run_id);

CREATE INDEX IF NOT EXISTS idx_filter_quality_runs_window
    ON filter_quality_evaluator.t_filter_quality_runs (
        news_window_start_at,
        news_window_end_at,
        created_at DESC
    );

CREATE INDEX IF NOT EXISTS idx_filter_quality_runs_fingerprint_created
    ON filter_quality_evaluator.t_filter_quality_runs (
        filter_config_fingerprint,
        created_at DESC,
        run_id
    );

CREATE TABLE IF NOT EXISTS filter_quality_evaluator.t_filter_quality_item_assessments (
    assessment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    evaluation_scope TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    filter_run_id_production TEXT NOT NULL,
    filter_run_id_simulation TEXT NOT NULL,
    production_filter_outcome TEXT NOT NULL,
    simulation_filter_outcome TEXT NOT NULL,
    is_disagreement BOOLEAN NOT NULL,
    rejection_reason_code TEXT,
    item_status TEXT NOT NULL,
    item_error_code TEXT,
    item_error_details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    classification_label TEXT,
    classification_confidence NUMERIC(6, 5),
    rationale TEXT,
    probable_cause TEXT,
    improvement_suggestion TEXT,
    suggestion_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_model TEXT,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_filter_quality_item_assessments_run_article
        UNIQUE (run_id, article_id),
    CONSTRAINT fk_filter_quality_item_assessments_run
        FOREIGN KEY (run_id)
        REFERENCES filter_quality_evaluator.t_filter_quality_runs (run_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_filter_quality_item_assessments_article
        FOREIGN KEY (article_id)
        REFERENCES news_fetcher.t_input_news_articles (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_filter_quality_item_assessments_prod_result
        FOREIGN KEY (filter_run_id_production, article_id)
        REFERENCES news_fetcher.t_news_filter_results (filter_run_id, article_id),
    CONSTRAINT fk_filter_quality_item_assessments_sim_result
        FOREIGN KEY (filter_run_id_simulation, article_id)
        REFERENCES news_fetcher.t_news_filter_results (filter_run_id, article_id),
    CONSTRAINT ck_filter_quality_item_assessments_scope
        CHECK (evaluation_scope IN ('rejected_population', 'accepted_audit')),
    CONSTRAINT ck_filter_quality_item_assessments_prod_outcome
        CHECK (production_filter_outcome IN ('accepted', 'rejected')),
    CONSTRAINT ck_filter_quality_item_assessments_sim_outcome
        CHECK (simulation_filter_outcome IN ('accepted', 'rejected')),
    CONSTRAINT ck_filter_quality_item_assessments_item_status
        CHECK (item_status IN ('evaluated', 'failed')),
    CONSTRAINT ck_filter_quality_item_assessments_label_enum
        CHECK (
            classification_label IS NULL
            OR classification_label IN (
                'correctly_rejected',
                'incorrectly_rejected',
                'correctly_accepted',
                'incorrectly_accepted'
            )
        ),
    CONSTRAINT ck_filter_quality_item_assessments_failed_label_null
        CHECK (
            item_status <> 'failed'
            OR classification_label IS NULL
        ),
    CONSTRAINT ck_filter_quality_item_assessments_evaluated_fields
        CHECK (
            item_status <> 'evaluated'
            OR (
                classification_label IS NOT NULL
                AND classification_confidence IS NOT NULL
                AND classification_confidence >= 0
                AND classification_confidence <= 1
            )
        ),
    CONSTRAINT ck_filter_quality_item_assessments_scope_label_consistency
        CHECK (
            classification_label IS NULL
            OR (
                evaluation_scope = 'rejected_population'
                AND classification_label IN (
                    'correctly_rejected',
                    'incorrectly_rejected'
                )
            )
            OR (
                evaluation_scope = 'accepted_audit'
                AND classification_label IN (
                    'correctly_accepted',
                    'incorrectly_accepted'
                )
            )
        ),
    CONSTRAINT ck_filter_quality_item_assessments_probable_cause
        CHECK (
            probable_cause IS NULL
            OR probable_cause IN (
                'keyword_gap',
                'watchlist_coverage_gap',
                'dedupe_threshold_issue',
                'rule_conflict',
                'low_value_noise'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_filter_quality_item_assessments_run_scope_status
    ON filter_quality_evaluator.t_filter_quality_item_assessments (
        run_id,
        evaluation_scope,
        item_status,
        article_id
    );

CREATE INDEX IF NOT EXISTS idx_filter_quality_item_assessments_disagreement
    ON filter_quality_evaluator.t_filter_quality_item_assessments (
        run_id,
        is_disagreement,
        simulation_filter_outcome,
        article_id
    );

CREATE INDEX IF NOT EXISTS idx_filter_quality_item_assessments_rejection_reason
    ON filter_quality_evaluator.t_filter_quality_item_assessments (
        run_id,
        rejection_reason_code,
        article_id
    );
