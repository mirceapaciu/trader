# Filter Quality Evaluator Data Model

Tables owned by the Filter Quality Evaluator component.
PostgreSQL schema: `filter_quality_evaluator`.

## Logical Model

### `t_filter_quality_runs`

Purpose:
- Durable metadata and summary for one on-demand quality evaluation run.

Logical fields:
- `run_id` (primary key): stable run identity.
- `news_window_start_at`: evaluated news-time lower bound (UTC).
- `news_window_end_at`: evaluated news-time upper bound (UTC).
- `filter_config_fingerprint`: optional exact-match fingerprint constraint.
- `trigger_mode`: run trigger source, for example `manual_cli`.
- `accepted_audit_enabled`: whether accepted-item audit mode is enabled for this run.
- `accepted_audit_sample_size`: configured accepted-item sample size for this run.
- `status`: `queued`, `running`, `completed`, `failed`, or `canceled`.
- `error_code`: nullable machine-readable terminal error.
- `accepted_count`: accepted population count for dataset slice.
- `rejected_count`: rejected population count for dataset slice.
- `incorrect_rejection_count`: rejected items classified as incorrectly rejected.
- `correct_rejection_count`: rejected items classified as correctly rejected.
- `accepted_items_sampled`: accepted items actually evaluated in this run.
- `incorrect_accepted_count`: accepted items classified as incorrectly accepted.
- `correct_accepted_count`: accepted items classified as correctly accepted.
- `incorrect_accepted_rate_estimate`: estimated false-positive rate over accepted population.
- `estimated_noisy_accepted_count`: estimated noisy accepted items in the full run population.
- `estimated_downstream_tokens_wasted`: estimated downstream trading-LLM token waste.
- `estimated_downstream_llm_cost_wasted`: estimated downstream trading-LLM cost waste.
- `summary_json`: structured run-level metrics and recommendation aggregates.
- `created_at`: creation timestamp.
- `started_at`: execution start timestamp.
- `finished_at`: execution completion timestamp.

Behavioral constraints:
- One immutable row per run id.
- Status transitions are monotonic and audit-safe.
- Count fields must be non-negative.

### `t_filter_quality_item_assessments`

Purpose:
- Per-item classification and recommendation output for each evaluated rejected item.

Logical fields:
- `assessment_id` (primary key): stable assessment identity.
- `run_id`: parent run identity.
- `source_article_id`: evaluated article identifier from NewsFetcher.
- `source_key`: source stream key from NewsFetcher.
- `rejection_reason_code`: NewsFetcher rejection reason when the item was rejected; nullable for accepted-item audits.
- `classification_label`: `correctly_rejected`, `incorrectly_rejected`, `correctly_accepted`, or `incorrectly_accepted`.
- `classification_confidence`: normalized confidence in [0, 1].
- `rationale`: explanation text.
- `probable_cause`: normalized cause category.
- `improvement_suggestion`: actionable recommendation text.
- `suggestion_json`: structured recommendation payload.
- `llm_model`: model identifier used for evaluation.
- `tokens_used`: optional token usage.
- `evaluated_at`: assessment timestamp.

Behavioral constraints:
- Each run can include at most one final assessment per `source_article_id`.
- `run_id` must reference an existing run.
- Classification and confidence must follow contract bounds.

## External Read Dependencies (Not Owned)

The component reads but does not own:
- input corpus articles from `news_fetcher.t_input_news_articles`.
- production and simulation filter outcomes from `news_fetcher.t_news_filter_results`.
- filter execution metadata from `news_fetcher.t_news_filter_runs`.
- accepted production subset from `news_fetcher.t_news_articles`.

## Notes

- Executable PostgreSQL DDL should be added under `src/product_components/filter_quality_evaluator/db/schema.sql` during implementation.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL or migrations.
