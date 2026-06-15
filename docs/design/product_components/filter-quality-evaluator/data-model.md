# Filter Quality Evaluator Data Model

Tables owned by the Filter Quality Evaluator component.
PostgreSQL schema: `filter_quality_evaluator`.

## Logical Model

### `t_filter_quality_runs`

Purpose:
- Durable metadata and summary for one on-demand quality evaluation run.

Logical fields:
- `run_id` (primary key): stable run identity created at trigger time.
- `news_window_start_at`: evaluated news-time lower bound (UTC).
- `news_window_end_at`: evaluated news-time upper bound (UTC).
- `dataset_snapshot_hash`: deterministic hash of the selected dataset membership.
- `filter_config_fingerprint`: optional exact-match fingerprint constraint.
- `trigger_mode`: run trigger source (`manual_cli` in v1).
- `run_note`: optional operator note from trigger payload.
- `accepted_audit_enabled`: whether accepted-item audit mode is enabled for this run.
- `accepted_audit_sample_size`: configured accepted-item sample size for this run.
- `status`: `running`, `completed`, or `failed`.
- `error_code`: nullable machine-readable terminal error.
- `error_details_json`: nullable structured terminal error context.
- `dataset_input_count`: number of input candidates in the selected window/fingerprint slice.
- `dataset_rejected_count`: rejected population count for the selected slice.
- `dataset_accepted_count`: accepted population count for the selected slice.
- `rejected_items_evaluated`: rejected items actually evaluated by LLM.
- `accepted_items_sampled`: accepted items actually evaluated in this run.
- `correctly_rejected_count`: rejected items classified as correctly rejected.
- `incorrectly_rejected_count`: rejected items classified as incorrectly rejected.
- `correctly_accepted_count`: accepted items classified as correctly accepted.
- `incorrectly_accepted_count`: accepted items classified as incorrectly accepted.
- `rejection_precision_proxy`: proxy precision metric for rejected items.
- `incorrectly_accepted_rate_estimate`: estimated false-positive rate over accepted population.
- `token_budget_limit`: configured per-run token ceiling.
- `summary_json`: structured run-level metrics and recommendation aggregates.
- `recommendation_summary_md`: human-readable recommendation summary for operators.
- `created_at`: row creation timestamp.
- `started_at`: execution start timestamp.
- `finished_at`: terminal timestamp.

Behavioral constraints:
- One immutable row per run id.
- Status transitions are monotonic and audit-safe.
- Count fields must be non-negative.

### `t_filter_quality_item_assessments`

Purpose:
- Per-item classification and recommendation output for each evaluated article in rejected-population and accepted-audit scopes.

Logical fields:
- `assessment_id` (primary key): stable assessment identity.
- `run_id`: parent run identity.
- `article_id`: evaluated article identifier from NewsFetcher.
- `evaluation_scope`: `rejected_population` or `accepted_audit`.
- `source`: source/provider identifier copied from input corpus.
- `published_at`: publication timestamp copied from input corpus.
- `filter_run_id_production`: production filter run used for baseline lookup.
- `filter_run_id_simulation`: simulation filter run used for comparison.
- `production_filter_outcome`: baseline filter outcome (`accepted` or `rejected`).
- `simulation_filter_outcome`: simulation filter outcome (`accepted` or `rejected`).
- `is_disagreement`: whether production and simulation outcomes differ.
- `rejection_reason_code`: rejection reason selected from simulation result when rejected; nullable for accepted-item audits.
- `item_status`: `evaluated` or `failed`.
- `item_error_code`: nullable machine-readable per-item evaluation error code.
- `item_error_details_json`: nullable structured per-item error context.
- `classification_label`: `correctly_rejected`, `incorrectly_rejected`, `correctly_accepted`, or `incorrectly_accepted`.
- `classification_confidence`: normalized confidence in [0, 1].
- `rationale`: explanation text.
- `probable_cause`: normalized cause category.
- `improvement_suggestion`: actionable recommendation text.
- `suggestion_json`: structured recommendation payload.
- `llm_model`: model identifier used for evaluation.
- `evaluated_at`: assessment timestamp.

Behavioral constraints:
- Each run can include at most one final assessment per `article_id`.
- `run_id` must reference an existing run.
- Classification and confidence must follow contract bounds.

## Physical Contract (Required for v1)

The following physical definitions are the required output contract for implementation.

### `filter_quality_evaluator.t_filter_quality_runs`

Columns:
- `run_id TEXT PRIMARY KEY`
- `news_window_start_at TIMESTAMPTZ NOT NULL`
- `news_window_end_at TIMESTAMPTZ NOT NULL`
- `dataset_snapshot_hash TEXT NOT NULL`
- `filter_config_fingerprint TEXT NULL`
- `trigger_mode TEXT NOT NULL DEFAULT 'manual_cli'`
- `run_note TEXT NULL`
- `accepted_audit_enabled BOOLEAN NOT NULL DEFAULT FALSE`
- `accepted_audit_sample_size INTEGER NULL`
- `status TEXT NOT NULL`
- `error_code TEXT NULL`
- `error_details_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `dataset_input_count INTEGER NOT NULL DEFAULT 0`
- `dataset_rejected_count INTEGER NOT NULL DEFAULT 0`
- `dataset_accepted_count INTEGER NOT NULL DEFAULT 0`
- `rejected_items_evaluated INTEGER NOT NULL DEFAULT 0`
- `accepted_items_sampled INTEGER NOT NULL DEFAULT 0`
- `correctly_rejected_count INTEGER NOT NULL DEFAULT 0`
- `incorrectly_rejected_count INTEGER NOT NULL DEFAULT 0`
- `correctly_accepted_count INTEGER NOT NULL DEFAULT 0`
- `incorrectly_accepted_count INTEGER NOT NULL DEFAULT 0`
- `rejection_precision_proxy NUMERIC(6,5) NULL`
- `incorrectly_accepted_rate_estimate NUMERIC(6,5) NULL`
- `token_budget_limit INTEGER NOT NULL`
- `summary_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `recommendation_summary_md TEXT NOT NULL DEFAULT ''`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
- `finished_at TIMESTAMPTZ NULL`

Check constraints:
- `trigger_mode IN ('manual_cli')`
- `status IN ('running', 'completed', 'failed')`
- `news_window_start_at < news_window_end_at`
- `dataset_snapshot_hash <> ''`
- `accepted_audit_sample_size IS NULL OR accepted_audit_sample_size > 0`
- `accepted_audit_enabled = TRUE OR accepted_audit_sample_size IS NULL`
- all count fields are `>= 0`
- `token_budget_limit > 0`
- `rejection_precision_proxy IS NULL OR (rejection_precision_proxy >= 0 AND rejection_precision_proxy <= 1)`
- `incorrectly_accepted_rate_estimate IS NULL OR (incorrectly_accepted_rate_estimate >= 0 AND incorrectly_accepted_rate_estimate <= 1)`
- terminal completion consistency:
	- if `status = 'running'`, then `finished_at IS NULL`
	- if `status IN ('completed', 'failed')`, then `finished_at IS NOT NULL`

Required indexes:
- run-time listing: `(status, created_at DESC, run_id)`
- window query: `(news_window_start_at, news_window_end_at, created_at DESC)`
- fingerprint compare query: `(filter_config_fingerprint, created_at DESC, run_id)`

### `filter_quality_evaluator.t_filter_quality_item_assessments`

Columns:
- `assessment_id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL`
- `article_id TEXT NOT NULL`
- `evaluation_scope TEXT NOT NULL`
- `source TEXT NOT NULL`
- `published_at TIMESTAMPTZ NOT NULL`
- `filter_run_id_production TEXT NOT NULL`
- `filter_run_id_simulation TEXT NOT NULL`
- `production_filter_outcome TEXT NOT NULL`
- `simulation_filter_outcome TEXT NOT NULL`
- `is_disagreement BOOLEAN NOT NULL`
- `rejection_reason_code TEXT NULL`
- `item_status TEXT NOT NULL`
- `item_error_code TEXT NULL`
- `item_error_details_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `classification_label TEXT NULL`
- `classification_confidence NUMERIC(6,5) NULL`
- `rationale TEXT NULL`
- `probable_cause TEXT NULL`
- `improvement_suggestion TEXT NULL`
- `suggestion_json JSONB NOT NULL DEFAULT '{}'::jsonb`
- `llm_model TEXT NULL`
- `evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

Foreign keys:
- `run_id -> filter_quality_evaluator.t_filter_quality_runs(run_id) ON DELETE CASCADE`

External lineage fields:
- `article_id`, `filter_run_id_production`, and `filter_run_id_simulation` are copied ids from the NewsFetcher evaluation dataset API/export.
- They must not be enforced with cross-schema foreign keys. NewsFetcher-owned tables remain private to NewsFetcher.

Uniqueness:
- `UNIQUE (run_id, article_id)`

Check constraints:
- `evaluation_scope IN ('rejected_population', 'accepted_audit')`
- `production_filter_outcome IN ('accepted', 'rejected')`
- `simulation_filter_outcome IN ('accepted', 'rejected')`
- `item_status IN ('evaluated', 'failed')`
- if `item_status = 'failed'`, then `classification_label IS NULL`
- if `item_status = 'evaluated'`, then:
	- `classification_label IS NOT NULL`
	- `classification_confidence IS NOT NULL`
	- `classification_confidence >= 0 AND classification_confidence <= 1`
- `classification_label IS NULL OR classification_label IN ('correctly_rejected', 'incorrectly_rejected', 'correctly_accepted', 'incorrectly_accepted')`
- scope-label consistency:
	- `evaluation_scope = 'rejected_population'` allows only `correctly_rejected` or `incorrectly_rejected`
	- `evaluation_scope = 'accepted_audit'` allows only `correctly_accepted` or `incorrectly_accepted`
- `probable_cause IS NULL OR probable_cause IN ('keyword_gap', 'watchlist_coverage_gap', 'dedupe_threshold_issue', 'rule_conflict', 'low_value_noise')`

Required indexes:
- by run and scope: `(run_id, evaluation_scope, item_status, article_id)`
- disagreement triage: `(run_id, is_disagreement, simulation_filter_outcome, article_id)`
- rejected reason analysis: `(run_id, rejection_reason_code, article_id)`

Write semantics:
- Insert run row first with `status='running'`.
- Insert item rows incrementally as each item reaches `evaluated` or `failed` terminal per-item state.
- Update run aggregate counters and summaries in-place during execution.
- Finalize run with terminal `status` and `finished_at`; no further item writes after terminal transition.

## External Data Dependencies (Not Owned)

The component consumes but does not own NewsFetcher evaluation datasets:
- input corpus articles.
- production and simulation filter outcomes.
- filter execution metadata.
- accepted production subset.

These datasets must be supplied through a NewsFetcher-owned evaluation API/export. Filter Quality Evaluator must not query NewsFetcher-owned tables directly.

## Notes

- Executable PostgreSQL DDL should be added under `src/product_components/filter_quality_evaluator/db/schema.sql` during implementation.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL or migrations.
