# ThesisBuilder Data Model

Tables owned by the ThesisBuilder component.
PostgreSQL schema: `thesis_builder`.

## Logical Model

### `t_thesis_cards`

Purpose:
- Canonical trade-justification object produced from news analyses.
- Durable ThesisBuilder-owned source of truth for generated thesis cards.

Logical fields:
- `id` (primary key): thesis card identity.
- `idempotency_key`: deterministic card identity for retry-safe creation.
- `ticker`: target instrument symbol.
- `exchange_code`: target exchange identifier (prefer MIC, for example `XNAS`, `XNYS`).
- `direction`: proposed direction (`buy`, `sell`, `hold`).
- `time_horizon`: strategy horizon descriptor.
- `strategy`: thesis strategy that produced the card, such as `sentiment_momentum`, `event_driven`, `sector_rotation`, `contrarian_reversal`, or `trend_follow`.
- `evidence`: exactly three evidence entries with article references.
- `source_analysis_ids`: analysis records used to create or validate the card.
- `confidence`: normalized confidence in [0, 1].
- `risk_max_loss_usd`: max tolerated loss for this thesis.
- `risk_stop_condition`: stop condition expression.
- `risk_invalidation_condition`: thesis invalidation expression.
- `market_context_status`: optional status returned by the MarketData component API (`fresh`, `delayed`, `stale`, or `missing`).
- `market_context_as_of`: optional timestamp of the copied market context snapshot.
- `market_context_snapshot`: optional JSON copy of the MarketData context used for strategy validation, confidence, or risk-box generation.
- `validation_status`: deterministic validation result (`valid` or `rejected`).
- `validation_errors`: optional machine-readable validation failures.
- `rejection_reason_code`: optional machine-readable reason for rejected cards, including `stale_evidence`.
- `max_evidence_age_seconds`: maximum age, in seconds, of any evidence article at card validation time.
- `allowed_max_evidence_age_seconds`: freshness limit used for validation.
- `evidence_age_exceeded_seconds`: amount by which evidence age exceeded the freshness limit; zero or null for non-stale cards.
- `story_narrative`: optional seed-stable story narrative copied from the evidence window that produced the card.
- `signal_published_at`: optional timestamp when the executable signal was published.
- `expires_at`: card expiry timestamp.
- `created_at`: card creation timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- Evidence, confidence, risk, freshness, and review invariants are governed by `docs/design/shared/product-constraint.md`.
- All evidence items must reference accepted article ids received from NewsFetcher event payloads or the NewsFetcher API. ThesisBuilder must not validate evidence by querying NewsFetcher-owned tables directly.
- Evidence must be traceable to at least one `t_news_analyses` row through `source_analysis_ids`.
- Market context audit data is copied from the MarketData component API response; ThesisBuilder must not query MarketData-owned tables directly.
- Valid cards must receive a matching shared review state through the shared review contract.
- Initially, valid cards are preapproved by system policy with `decision_state=approved`, `reviewed_by=system_policy`, and a review reason identifying the policy version.
- Cards that fail product-constraint validation are persisted only as non-executable rejected records for audit, if persisted at all; they must not be published as executable signals.
- Cards generated only to estimate missed opportunities from old news use `validation_status=rejected` and `rejection_reason_code=stale_evidence`; the UI labels these as `stale`.
- TradeExecutor must not infer approval from this table alone; executable state is determined by shared review state plus freshness.

### `t_card_synthesis_verdicts`

Purpose:
- Durable audit trail for the optional card-synthesis LLM pass that runs after an evidence window
  satisfies deterministic gates and before an executable thesis card is created.

Logical fields:
- `id` (primary key): verdict row identity.
- `evidence_window_id`: ThesisBuilder evidence window that triggered synthesis.
- `card_id`: nullable thesis card id; populated for approved synthesis verdicts that create a card.
- `ticker`, `exchange_code`, `strategy`, `direction`: copied candidate identity.
- `verdict`: `approve`, `reject`, `invalid`, or `unavailable`.
- `reason_code`: optional machine-readable rejection or failure reason such as
  `synthesis_rejected`, `synthesis_invalid`, or `synthesis_unavailable`.
- `confidence`: synthesis confidence when the model returned one.
- `llm_model`, `max_output_tokens`: configured synthesis call metadata.
- `response_json`: raw structured synthesis response or failure payload for audit.
- `created_at`: row creation timestamp.

Behavioral constraints:
- Reject, invalid, and unavailable verdicts must not create executable thesis cards by default.
- Approved verdicts must be traceable to the card they created, and card confidence/risk/evidence
  fields should reflect the synthesis output when synthesis is enabled.
- This table is ThesisBuilder-owned audit data; downstream components consume executable card
  signals and shared review state, not synthesis rows directly.

### `t_card_corroborations`

Purpose:
- Records later valid analyses that match an unexpired satisfied card's story after the card is already frozen.

Logical fields:
- `id` (primary key): corroboration row identity.
- `card_id`: frozen thesis card that received corroborating coverage.
- `article_id`: incoming article identity.
- `analysis_id`: valid analysis row for the incoming article/instrument pair.
- `matched_at`: timestamp when story assignment matched the card.
- `created_at`: row creation timestamp.

Behavioral constraints:
- Corroboration rows do not mutate the card, publish signals, or alter shared review state.
- `(card_id, article_id)` is unique so repeated processing does not inflate corroboration counts.

### `t_story_assignments`

Purpose:
- Durable audit trail for story-scoping decisions made after analysis persistence and before evidence-window mutation.

Logical fields:
- `id` (primary key): assignment row identity.
- `analysis_id`: valid analysis being assigned; unique for idempotent audit updates.
- `article_id`: incoming article identity.
- `candidate_targets`: JSON list of candidate `window:<id>` and `card:<id>` targets shown to the assignment step.
- `chosen_target`: raw assignment result (`window:<id>`, `card:<id>`, or `new_story`) before deterministic verification.
- `resolved_target`: final target after deterministic verification. Cross-story `window:<id>` or `card:<id>` assignments are downgraded to `new_story`.
- `assignment_source`: `matched`, `new_story`, or `fallback`.
- `verification_status`: `skipped`, `passed`, or `downgraded`.
- `verification_reason_code`: verifier reason such as `story_event_mismatch` or
  `story_event_check_unavailable`.
- `verification_details_json`: bounded token-overlap audit details used by the verifier,
  including `event_check` of `same`, `different`, or `unavailable` for lexically
  inconclusive assignments.
- `llm_model`, `max_output_tokens`, `tokens_used`: configured assignment call metadata and token usage.
- `response_json`: raw structured assignment response when available.
- `error_code`: transport, parser, or schema error when fallback was used.
- `reprocess_run_id`: optional regeneration/reprocess scope.
- `created_at`: row creation timestamp.

### `t_news_analyses`

Purpose:
- Durable record of ThesisBuilder outputs for each analyzed news item and ticker.

Logical fields:
- `id` (primary key): analysis record identity.
- `article_id`: source article identity from NewsFetcher.
- `ticker`: analyzed instrument symbol.
- `exchange_code`: analyzed exchange identifier (prefer MIC, for example `XNAS`, `XNYS`).
- `sentiment`: sentiment score for the ticker in article context.
- `relevance`: relevance score for the ticker.
- `urgency`: urgency classification.
- `suggested_action`: suggested trading action.
- `strategy`: candidate thesis strategy from the validated LLM output or deterministic policy.
- `direction`: candidate direction (`buy`, `sell`, or `hold`).
- `event_type`: optional event classification used by event-driven analysis.
- `event_occurred_at`: optional timestamp of the underlying reported event, extracted by the LLM from the article text (null when the text does not date the event; unparseable values degrade to null). Existing rows are null. Feeds the effective evidence timestamp used by the retention cutoff and card freshness gate (behavior spec §3.3/§5, issue 260715-03).
- `subject_relation`: optional relationship between the article event and the instrument (`direct`, `supply_chain`, `customer_or_peer`, `macro_sector`, or `none`). Existing rows may be null; new full-analysis rows persist the parsed relation for funnel attribution.
- `price_impact_magnitude`: optional expected impact magnitude (`low`, `medium`, or `high`), anchored to the instrument's `atr_20d` (see behavior spec §3.3). Observe-only; not yet consumed by gates.
- `impact_horizon`: optional window over which the estimated impact is expected to be realized (`intraday`, `1d`, or `5d`). Observe-only.
- `reasoning`: optional explanatory reasoning text.
- `confidence`: confidence score.
- `market_context_status`: optional status returned by the MarketData component API (`fresh`, `delayed`, `stale`, or `missing`).
- `market_context_as_of`: optional timestamp of the copied market context snapshot.
- `market_context_snapshot`: optional JSON copy of the MarketData context used for scoring or strategy validation.
- `fundamentals_snapshot`: optional JSON copy of the company fundamentals (market cap, shares outstanding, TTM revenue, next earnings date) shown to the LLM for this analysis, copied from the MarketData `get_fundamentals` response for audit; null when fundamentals were unavailable.
- `is_market_moving`: deterministic/LLM-derived flag indicating that the article was considered market moving for this instrument.
- `validation_status`: deterministic output validation result (`valid` or `rejected`).
- `validation_errors`: optional machine-readable validation failures.
- `rejection_reason_code`: optional machine-readable reason for rejected or non-actionable analyses.
- `llm_model`: model identifier used for the analysis.
- `tokens_used`: optional token usage for the analysis call.
- `analyzed_at`: analysis timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- `article_id` must reference an accepted NewsFetcher article id from the event payload or NewsFetcher API response.
- Analysis records are append-oriented for auditability.
- Scores and classifications must be derived from deterministic thesis-building policy for identical inputs when deterministic mode is enabled.
- Invalid LLM output is persisted with `validation_status=rejected` and must not contribute to executable card creation.
- Market context audit data is copied from the MarketData component API response; ThesisBuilder must not query MarketData-owned tables directly.
- Each executable thesis card must be traceable to one or more analysis records.
- Analyses may be emitted without a thesis card when evidence is insufficient, conflicting, stale, or below confidence thresholds.

### `t_message_processing_events`

Purpose:
- Durable audit and monitoring record for every NewsFetcher queue message consumed by ThesisBuilder, including messages that are skipped before analysis.

Logical fields:
- `source_message_id`: Redis stream message id consumed from `news_raw_queue`.
- `event_id`: upstream event id when present.
- `article_id`: article identity derived from the payload or stream metadata.
- `outcome`: processing outcome (`analyzed`, `skipped`, or `failed_dlq`).
- `reason_code`: optional machine-readable reason such as `no_active_instrument` or `missing_article_payload`.
- `analyses_created`: number of `t_news_analyses` rows created while handling the message.
- `signals_published`: number of executable thesis signals published while handling the message.
- `processed_at`: timestamp when ThesisBuilder finished handling the message.
- `payload_json`: JSON copy of the consumed payload for audit/debugging.

Behavioral constraints:
- There is at most one processing-event row per source stream message id.
- Overview-level pipeline monitoring uses this table to count ThesisBuilder consumed/processed messages; analysis-specific charts continue to use `t_news_analyses`.
- Skipped messages are terminal from the consumer perspective and must be visible here even when no analysis row is created.

### `t_evidence_windows`

Purpose:
- Tracks bounded aggregation state while ThesisBuilder waits for enough evidence to make a trade decision.

Logical fields:
- `id` (primary key): evidence window identity.
- `ticker`: target instrument symbol.
- `exchange_code`: target exchange identifier.
- `strategy`: candidate strategy being evaluated.
- `direction`: candidate direction when known.
- `article_ids`: unique article ids currently in the window.
- `analysis_ids`: analysis ids currently in the window.
- `window_started_at`: `published_at` of the oldest article still retained in the rolling window.
- `last_evidence_at`: timestamp of most recent eligible article.
- `story_narrative`: optional seed-stable narrative used to identify the underlying story when story scoping is enabled.
- `status`: `collecting`, `satisfied`, or `rejected` (`expired` is a legacy value no longer produced).
- `status_reason`: optional machine-readable reason for terminal states.
- `created_at`: row creation timestamp.
- `updated_at`: row update timestamp.

Behavioral constraints:
- ThesisBuilder aggregates only until the product constraint has enough evidence for a trade decision.
- A window becomes `satisfied` as soon as it can produce a valid thesis card under `docs/design/shared/product-constraint.md`.
- The collection span is rolling: evidence older than the configured span relative to the latest analysis ages out of the window individually; the window itself never expires and keeps collecting.
- Window rows are operational state, not executable trading inputs.

## Analysis-History Export Contract (Consumed by Backtester)

ThesisBuilder owns a second read-only export, `ThesisAnalysisHistoryExporter`, so offline consumers
can study per-analysis records without querying ThesisBuilder-owned tables directly. This is the
analysis analogue of the card-history export below and exists specifically so the Backtester
impact-calibration report never reaches into the `thesis_builder` schema.

Selection:
- A time window over analysis `analyzed_at`.
- Restricted to analyses with a `buy`/`sell` `direction` and a non-null `price_impact_magnitude`
  (the population the calibration study measures), with optional `event_type`, magnitude, and
  `validation_status='valid'` filters. Rejected analyses are included by default so the study is not
  biased toward only the analyses that became cards.

Per exported analysis, the contract returns `analysis_id`, `ticker`, `exchange_code`, `direction`,
`event_type`, `subject_relation`, `price_impact_magnitude`, `impact_horizon`, `validation_status`, the article
`published_at` (from the retained article snapshot), and the `atr_20d` the realized move is
normalized against (from the retained market-context snapshot).

Constraints:
- Read-only; must not expose mutation of ThesisBuilder state.
- The `thesis_schema` argument may target the production schema or a regeneration `sim_bt_<run_id>`
  copy, so the same report runs over live analyses or a regenerated funnel.

## Card-History Export Contract (Consumed by Backtester)

ThesisBuilder owns a read-only card-history export API/contract so offline consumers (the Backtester,
Phase 7) can replay historical cards without querying ThesisBuilder-owned tables directly. This is the
card analogue of the NewsFetcher evaluation dataset export consumed by the Filter Quality Evaluator.

Selection:
- A time window over card `created_at`.
- Optional filters on `validation_status` and `strategy`.

Per exported card, the contract returns:
- `id`, `ticker`, `exchange_code`, `direction`, `strategy`, `time_horizon`, `confidence`.
- Risk box: `risk_max_loss_usd`, `risk_stop_condition`, `risk_invalidation_condition`.
- `validation_status` and `rejection_reason_code` (so consumers can distinguish `approved`, `rejected`,
  and `stale_evidence` cards).
- `created_at`, `expires_at`, and `signal_published_at`.
- `evidence`: for each evidence article, its `article_id`, `published_at`, and `fetched_at`. The
  publication and ingestion timestamps are sourced from the article snapshot ThesisBuilder already
  retains in `t_news_analyses.article_snapshot`, so consumers can compute NewsFetcher and
  ThesisBuilder pipeline delays without a separate NewsFetcher export.
- `story_narrative` and `corroboration_count` for story-scoped cards.

Constraints:
- The export is read-only and must not expose mutation of ThesisBuilder state.
- Consumers copy the returned fields into their own audit tables for reproducibility; they must not
  hold a foreign-key dependency on ThesisBuilder tables.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/thesis_builder/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
