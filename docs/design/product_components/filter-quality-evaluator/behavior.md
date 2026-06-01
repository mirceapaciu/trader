# Filter Quality Evaluator Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the Filter Quality Evaluator process.

Filter Quality Evaluator responsibilities:
- Run on-demand filter simulations over the existing 30-day NewsFetcher input corpus after configuration changes.
- Compare simulation results against production baseline filter results.
- Evaluate quality of filtering decisions, especially disagreements and incorrect rejections.
- Optionally audit accepted items to estimate false-positive noise and downstream trading-LLM cost impact.
- Produce actionable recommendations for improving NewsFetcher filtering rules.
- Persist run outputs for auditability and comparison across configuration changes.

Out of scope:
- Continuous ingestion and filtering of live provider feeds.
- News publication to downstream trading queues.
- Trade decision generation and execution.

## 2. Process Contract

Process name: filter_quality_evaluator

Inputs:
- Input corpus articles from `news_fetcher.t_input_news_articles`.
- Production baseline filter results from `news_fetcher.t_news_filter_results`.
- Accepted production subset from `news_fetcher.t_news_articles` when needed for downstream impact checks.
- Run parameters (time window required, config fingerprint optional).
- LLM provider credentials and policy.

Outputs:
- Run summary rows in `filter_quality_evaluator.t_filter_quality_runs`.
- Per-item evaluation rows in `filter_quality_evaluator.t_filter_quality_item_assessments`.
- Human-readable recommendation summary in `filter_quality_evaluator.t_filter_quality_runs.recommendation_summary_md`.
- Concrete physical output contract is defined in `docs/design/product_components/filter-quality-evaluator/data-model.md`.

Delivery semantics:
- On-demand execution only. The process is not a continuously running consumer.
- Idempotent run creation for the evaluator run record based on an explicit run id generated at trigger time.

## 3. Triggering and Run Lifecycle
- Direct CLI execution of `filter_quality_evaluator`.
- No separate triggering process or queue trigger in v1.

Trigger payload:
- news_window_start_at (UTC)
- news_window_end_at (UTC) should be lower than the timestamp of the newest news
- filter_config_fingerprint (optional)
- filter_config_snapshot_json (optional; required when running a simulation with overrides not already present in the active NewsFetcher production configuration)
- run_note (optional)
- accepted_audit_enabled (optional, default false)
- accepted_audit_sample_size (optional, required when accepted_audit_enabled=true)

Lifecycle states:
1. running
2. completed
3. failed

Run policy:
- A run evaluates one immutable dataset snapshot defined by the parameters.
- A run may be retried by creating a new run id, not by mutating completed results.
- A direct CLI invocation creates exactly one run request.
- Evaluator `run_id` is unique per invocation and is not reused across retries.
- Production baseline `filter_run_id` is reused when `filter_config_fingerprint` is unchanged.
- When production fingerprint changes, a new production `filter_run_id` is created.

## 4. Evaluation Dataset Rules

Dataset selection order:
1. Time window filter on `t_input_news_articles.published_at`.
2. Optional exact match on production or simulation `filter_config_fingerprint`.

Population contract:
- Input population: structurally valid normalized candidates from the last retained 30 days.
- Production baseline: NewsFetcher results recorded under a `production` filter run.
- Simulation population: results written under a new `simulation` filter run created for the requested configuration.

Dataset join and matching rules:
- Canonical join key is `article_id`, sourced from `news_fetcher.t_input_news_articles.id`.
- The evaluator first materializes the selected input slice from `t_input_news_articles`, then left-joins the corresponding baseline and simulation rows from `t_news_filter_results`.
- The evaluator creates the simulation filter run with a run-scoped immutable `filter_config_snapshot_json` payload; this payload is stored on the simulation run row and is never written into global NewsFetcher configuration.
- Baseline rows are selected from exactly one production filter run:
	- if `filter_config_fingerprint` is provided, use the unique production run with the same fingerprint;
	- otherwise use the most recently created production run that covers the selected news window.
- Simulation rows are selected from the single simulation run created for the current evaluator run id.
- A row participates in comparison only when the input article exists and both baseline and simulation rows can be matched by `(filter_run_id, article_id)`.
- If the baseline row is missing, the item is recorded as a failed item with `item_error_code = missing_production_result`.
- If the simulation row is missing, the item is recorded as a failed item with `item_error_code = missing_simulation_result`.
- If both rows exist, the evaluator compares `filter_outcome` values to determine `is_disagreement`.
- If either row is present more than once for the same `(filter_run_id, article_id)`, the run fails with a hard data-integrity error because `t_news_filter_results` must be unique on that pair.
- Rejection reason analysis uses the baseline row for production behavior and the simulation row for proposed behavior; the simulation row wins for the saved `rejection_reason_code` when the simulated outcome is `rejected`.

Rejection reason taxonomy expected from NewsFetcher:
- rejected_structural_invalid
- rejected_excluded_keyword
- rejected_not_relevant
- rejected_strong_duplicate
- rejected_soft_duplicate

DB-first source of truth:
- The analyzer reads datasets from PostgreSQL tables, not directly from queue streams.
- Simulation writes into NewsFetcher-owned run/result tables and must not affect the production pipeline state.

## 5. LLM Classification Rules

For each rejected item, the analyzer produces:
- classification_label: correctly_rejected or incorrectly_rejected
- classification_confidence: normalized value in [0, 1]
- rationale: concise explanation referencing observable article attributes
- improvement_suggestion: concrete filter adjustment suggestion

When accepted audit mode is enabled, the analyzer also evaluates a sampled subset of accepted items and produces:
- classification_label: correctly_accepted or incorrectly_accepted
- classification_confidence: normalized value in [0, 1]
- rationale: concise explanation referencing observable article attributes
- improvement_suggestion: concrete filter adjustment suggestion focused on reducing low-value accepted noise

Accepted audit sampling rules:
- Sampling is optional and off by default.
- Sampling is applied only to accepted items from the selected dataset window.
- Sample selection must be deterministic for a given run id and dataset snapshot.
- If accepted population is smaller than sample size, evaluate all accepted items.

Minimum prompt context for each rejected item:
- normalized headline and summary
- source and occurred_at
- entities and attributes when available
- rejection reason code and filter configuration context

Cost control policy:
- Prioritize disagreement cases between production and simulation in bounded batches.
- Enforce per-run token ceiling.
- Fail closed with run status failed when budget is exhausted before completion.
- Accepted audit consumes the remaining per-run budget after rejected-item priority work.

## 6. Recommendation Generation

The analyzer must provide both item-level and run-level guidance.

Item-level output for incorrectly rejected items:
- probable cause category (keyword gap, watchlist coverage gap, dedupe threshold issue, rule conflict)
- recommended change with impact hint

Run-level output:
- rejection precision proxy
- top rejection-reason error drivers
- grouped recommendations ordered by estimated impact
- accepted_audit_enabled flag and accepted_audit_sample_size
- accepted_items_total and accepted_items_sampled
- incorrectly_accepted_rate_estimate
- estimated_noisy_accepted_count
- estimated_downstream_tokens_wasted
- estimated_downstream_llm_cost_wasted

## 7. Configuration Fingerprint Contract

Fingerprint purpose:
- Support quality comparisons after NewsFetcher configuration changes.

Fingerprint scope (mandatory full effective context):
- include_keywords
- exclude_keywords
- watchlist snapshot or version reference
- dedupe policy settings (algorithm, threshold, lookback)

Fingerprint behavior:
- Deterministic and stable for equivalent effective context.
- Stored on both accepted and rejected source records.
- Run filter may constrain evaluation to one fingerprint value.

## 8. Operational Constraints

Runtime profile:
- Expensive, operator-driven workload.
- Not part of the NewsFetcher polling loop.

Reliability:
- Partial per-item failures are recorded and do not invalidate completed item evaluations.
- Hard failures mark run status failed with machine-readable error code.

Security and compliance:
- Secrets must be sourced from environment variables.
- Input and output data must remain within project data stores.

## 9. Integration Boundaries

Producer components:
- NewsFetcher writes the input corpus and production baseline filter outcomes.

Consumer components:
- Filter Quality Evaluator reads the input corpus, creates simulation filter runs/results, and stores quality assessments.
- Monitoring UI may consume analyzer run summaries in a later phase.

No direct coupling requirements:
- TradeExecutor and AnalyzerWorker trading pipeline do not depend on filter quality evaluator runs.
