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
- `signal_published_at`: optional timestamp when the executable signal was published.
- `expires_at`: card expiry timestamp.
- `created_at`: card creation timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- Evidence, confidence, risk, freshness, and review invariants are governed by `docs/design/shared/product-constraint.md`.
- All evidence items must reference valid `news_fetcher.t_news_articles` rows.
- Evidence must be traceable to at least one `t_news_analyses` row through `source_analysis_ids`.
- Market context audit data is copied from the MarketData component API response; ThesisBuilder must not query MarketData-owned tables directly.
- Valid cards must receive a matching shared review row in `shared.t_thesis_card_reviews`.
- Initially, valid cards are preapproved by system policy with `decision_state=approved`, `reviewed_by=system_policy`, and a review reason identifying the policy version.
- Cards that fail product-constraint validation are persisted only as non-executable rejected records for audit, if persisted at all; they must not be published as executable signals.
- TradeExecutor must not infer approval from this table alone; executable state is determined by shared review state plus freshness.

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
- `price_impact_magnitude`: optional expected impact magnitude (`low`, `medium`, or `high`).
- `reasoning`: optional explanatory reasoning text.
- `confidence`: confidence score.
- `market_context_status`: optional status returned by the MarketData component API (`fresh`, `delayed`, `stale`, or `missing`).
- `market_context_as_of`: optional timestamp of the copied market context snapshot.
- `market_context_snapshot`: optional JSON copy of the MarketData context used for scoring or strategy validation.
- `validation_status`: deterministic output validation result (`valid` or `rejected`).
- `validation_errors`: optional machine-readable validation failures.
- `rejection_reason_code`: optional machine-readable reason for rejected or non-actionable analyses.
- `llm_model`: model identifier used for the analysis.
- `tokens_used`: optional token usage for the analysis call.
- `analyzed_at`: analysis timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- `article_id` must reference an existing NewsFetcher article.
- Analysis records are append-oriented for auditability.
- Scores and classifications must be derived from deterministic thesis-building policy for identical inputs when deterministic mode is enabled.
- Invalid LLM output is persisted with `validation_status=rejected` and must not contribute to executable card creation.
- Market context audit data is copied from the MarketData component API response; ThesisBuilder must not query MarketData-owned tables directly.
- Each executable thesis card must be traceable to one or more analysis records.
- Analyses may be emitted without a thesis card when evidence is insufficient, conflicting, stale, or below confidence thresholds.

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
- `window_started_at`: timestamp of first eligible article.
- `last_evidence_at`: timestamp of most recent eligible article.
- `status`: `collecting`, `satisfied`, `expired`, or `rejected`.
- `status_reason`: optional machine-readable reason for terminal states.
- `created_at`: row creation timestamp.
- `updated_at`: row update timestamp.

Behavioral constraints:
- ThesisBuilder aggregates only until the product constraint has enough evidence for a trade decision.
- A window becomes `satisfied` as soon as it can produce a valid thesis card under `docs/design/shared/product-constraint.md`.
- A window becomes `expired` when the configured evidence collection horizon is exceeded.
- Window rows are operational state, not executable trading inputs.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product_components/thesis_builder/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
