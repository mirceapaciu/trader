# ThesisBuilder Behavior Specification

## 1. Purpose and Scope

This file defines the runtime behavior owned by the ThesisBuilder process.

ThesisBuilder responsibilities:
- Consume accepted news events from `news_raw_queue`.
- Produce durable per-article and per-instrument analysis records.
- Aggregate evidence only until a trade decision can be made under the product constraint.
- Create canonical thesis cards with ThesisBuilder-owned initial risk boxes.
- Write the initial shared thesis-card review state.
- Publish only validated, approved, fresh thesis-card signals to `signal_queue`.

Out of scope:
- Fetching, normalizing, filtering, or deduplicating news.
- Final broker risk checks, order sizing, order routing, and execution.
- UI approval workflows beyond writing the initial review state.

The shared product constraint in `docs/design/shared/product-constraint.md` is authoritative. ThesisBuilder must fail closed when that constraint cannot be satisfied.

## 2. Process Contract

Process name:
- `thesis_builder`

Inputs:
- Accepted news envelopes from `news_raw_queue`.
- Existing accepted articles in `news_fetcher.t_news_articles`.
- Active watchlist and instrument metadata from shared tables.
- Current and historical market context from the MarketData cache when required by strategy policy.
- ThesisBuilder configuration and shared queue/database settings.

Outputs:
- `thesis_builder.t_news_analyses` rows.
- `thesis_builder.t_evidence_windows` operational aggregation state.
- `thesis_builder.t_thesis_cards` rows.
- `shared.t_thesis_card_reviews` rows for cards that pass validation.
- Thesis-card signal envelopes on `signal_queue`.
- Failed envelopes on `failed_messages_dlq` after retry exhaustion.

Delivery semantics:
- Consume with at-least-once delivery.
- Acknowledge news messages only after analysis persistence and any required card/review/signal writes are complete.
- Use idempotency keys based on article id, instrument identity, strategy, and evidence set to avoid duplicate cards during retries.

## 3. Analysis Flow

For each accepted news event:

1. Resolve eligible instruments using the article ticker list plus shared instrument aliases.
2. Skip instruments that are not active in the shared watchlist.
3. Run deterministic prechecks for article freshness, source quality, duplicate evidence, and obvious non-market relevance.
4. Resolve market context from `market_data.t_market_context_snapshots` when the candidate strategy requires price-derived validation.
5. Call the configured LLM only when deterministic prechecks leave a plausible trading impact.
6. Persist one `t_news_analyses` row per analyzed article and instrument.
7. Add eligible analyses to an evidence window keyed by instrument, strategy, and candidate direction.
8. Attempt thesis-card creation immediately after each window update.

ThesisBuilder must store analyses even when no thesis card is created, so rejected, weak, stale, or conflicting evidence remains auditable.

## 3.1 Market Context Policy

News evidence remains the primary source for thesis-card evidence bullets. Market context is used to validate strategy fit, confidence, and risk-box construction; it does not replace the required news evidence from `docs/design/shared/product-constraint.md`.

Market context may include:
- Current price.
- Previous close.
- Intraday change.
- 1-day, 5-day, and 20-day returns.
- 20-day or 30-day volatility or ATR.
- Current volume relative to average volume.
- Moving averages such as 20-day and 50-day averages.
- Recent high, recent low, drawdown, and support or resistance levels.
- Optional sector, index, or peer relative movement.

Strategy market-data requirements:
- `event_driven`: market context is optional, but should be used when available to improve risk-box precision and avoid already-priced moves.
- `sentiment_momentum`: market context is recommended; missing context should lower confidence unless the news evidence is unusually strong and time-sensitive.
- `sector_rotation`: requires enough peer, sector, or index context to support any rotation claim.
- `contrarian_reversal`: requires recent and historical market context. ThesisBuilder must not create a contrarian reversal card unless price evolution shows an overextended move, stabilization or reversal evidence, and a deterministic tight risk box.
- `trend_follow`: requires historical market context sufficient to establish that the move is a durable trend rather than a short-lived news reaction.

When a strategy requires market context and that context is unavailable, stale, or insufficient, ThesisBuilder must fail closed for that strategy. It may still create a different strategy card from the same news evidence only if that alternate strategy satisfies its own requirements.

## 4. Evidence Aggregation

ThesisBuilder aggregates only until enough evidence exists to make a trade decision.

Window satisfaction rules:
- The window must meet all evidence rules from `docs/design/shared/product-constraint.md`.
- The default required evidence count is read from `THESIS_CARD_REQUIRED_EVIDENCE_COUNT`.
- Evidence must support one coherent instrument, direction, strategy, and time horizon.
- Conflicting high-confidence evidence prevents card creation until the conflict is resolved by newer or stronger evidence.

Window terminal states:
- `satisfied`: a valid thesis card was created.
- `expired`: the collection horizon elapsed before sufficient evidence appeared.
- `rejected`: evidence is structurally invalid, contradictory, below confidence, or otherwise non-actionable.

The default collection horizon is `THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES=120`. This is a ceiling, not a delay target: if sufficient evidence arrives earlier, ThesisBuilder creates the card immediately.

## 5. Thesis-Card Creation

The default time horizon is `swing_1d_5d`.

ThesisBuilder may emit longer trend-following cards when the evidence indicates a durable trend rather than a short-lived news reaction. Longer trend-follow cards must:
- Use a distinct strategy value such as `trend_follow`.
- Set an explicit time horizon longer than `swing_1d_5d`, capped by `THESIS_BUILDER_TREND_FOLLOW_MAX_DAYS`.
- Include an invalidation condition that explains what would end the trend thesis.
- Remain subject to the same evidence and approval constraints as all other cards.

Card creation steps:
1. Select the best evidence set from the satisfied window.
2. Generate exactly the canonical card fields required by `docs/design/shared/product-constraint.md`.
3. Generate ThesisBuilder-owned initial risk fields: max loss, stop condition, and invalidation condition.
4. Validate the card deterministically.
5. Persist the thesis card.
6. Write the initial shared review state.
7. Publish the card signal only if validation passes and review state is approved.

Initially, all valid cards are preapproved by system policy. ThesisBuilder writes `shared.t_thesis_card_reviews.decision_state=approved`, `reviewed_by=system_policy`, and a review reason identifying the ThesisBuilder policy version. If `THESIS_BUILDER_INITIAL_REVIEW_POLICY=manual` is enabled later, valid cards are persisted but not published as executable signals until a UI/user approval exists.

## 6. Strategy Policy

Initial strategy priority:
1. `event_driven`
2. `sentiment_momentum`
3. `sector_rotation`
4. `contrarian_reversal`
5. `trend_follow`

Conflict rules:
- Event-driven cards win over lower-priority strategies when evidence is specific and high confidence.
- Opposing high-confidence strategies for the same instrument produce no executable card.
- Sector rotation may create a peer card only when the peer is active in the watchlist and the spillover rationale is explicit.
- Contrarian reversal requires stronger confidence than momentum, recent and historical price context, evidence of an overextended prior move, and a tighter risk box.
- Trend-follow cards require evidence that the effect is likely to persist beyond five trading days plus historical market context that supports a durable trend.

## 7. Risk Box Policy

ThesisBuilder owns the initial risk box because it understands the thesis.

Risk fields:
- `risk_max_loss_usd`: the maximum tolerated loss for the thesis.
- `risk_stop_condition`: price, news, or time condition that should stop the trade.
- `risk_invalidation_condition`: event or evidence that invalidates the thesis itself.

Default risk behavior:
- Use `THESIS_BUILDER_RISK_MAX_LOSS_USD` as the starting max loss ceiling.
- Lower the max loss for lower-confidence, contrarian, or sector-spillover cards.
- Never exceed TradeExecutor portfolio and position risk limits.
- Express stop and invalidation conditions in deterministic, auditable terms.

TradeExecutor remains responsible for final execution risk checks and may reject a card-derived decision even when ThesisBuilder approved the thesis card.

## 8. Publishing Contract

ThesisBuilder publishes a signal envelope to `signal_queue` only when:
- The thesis card passed deterministic validation.
- Shared review state is `approved`.
- `expires_at` is later than the publish time.
- The card has not already been published for the same idempotency key.

Signal payload requirements:
- `thesis_card_id`
- `ticker`
- `exchange_code`
- `direction`
- `time_horizon`
- `strategy`
- `confidence`
- `risk_box`
- `source_analysis_ids`
- `created_at`
- `expires_at`

Hold or rejected outcomes may be persisted for audit, but they are not executable trading signals.

## 9. Failure Handling

ThesisBuilder must fail closed:
- Missing or invalid evidence creates no executable card.
- Missing risk fields creates no executable card.
- Missing shared review write prevents signal publication.
- LLM timeout or provider failure retries according to shared queue retry policy, then dead-letters the message.
- PostgreSQL write failure prevents ACK and must not publish a signal.
- Signal publish failure leaves the message unacknowledged or writes a dead-letter according to retry state.

Failures should include concise machine-readable error codes and enough context to inspect the affected article, instrument, and evidence window.

## 10. Source Organization

Default implementation placement:
- Process entry point: `src/product_components/thesis_builder`.
- ThesisBuilder persistence SQL: `src/product_components/thesis_builder/db/schema.sql`.
- Product-specific strategy, thesis-card, risk-box, and orchestration logic: `src/product_components/thesis_builder`.
- Reusable event-ingestion or preprocessing primitives, if introduced, belong under `src/core_components`.

ThesisBuilder is a product component. It may depend on reusable core components, but core components must not depend on ThesisBuilder.

## 11. Acceptance Criteria

Implementation is acceptable when all are true:
- Accepted news can produce durable analysis records without producing trades prematurely.
- ThesisBuilder creates valid thesis cards only when the shared product constraint is satisfied.
- Valid cards are initially preapproved by system policy and receive shared review rows.
- Insufficient evidence is aggregated only until the evidence requirement is met or the window expires.
- Initial risk boxes are present and deterministic enough for audit.
- Signals published to `signal_queue` always reference a valid approved thesis card.
- Duplicate message delivery does not create duplicate executable cards.
- Required unit and integration tests pass.

## 12. Minimum Test Plan

Unit tests:
- Product-constraint validation for evidence count, article diversity, confidence range, risk fields, expiry, and review state.
- Evidence-window satisfaction, expiry, and rejection paths.
- Strategy conflict resolution.
- Risk-box generation for default swing, contrarian, sector rotation, and trend-follow cards.
- Idempotency key generation for retries.

Integration tests:
- Consume a news event, persist analyses, create a preapproved thesis card, and publish one signal.
- Replayed news event does not create a duplicate executable card.
- Insufficient evidence persists analysis but publishes no signal.
- Shared review write failure prevents signal publication.
- LLM failure retries and then dead-letters according to queue policy.
