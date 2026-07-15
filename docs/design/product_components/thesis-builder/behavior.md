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
- Accepted news envelopes from `news_raw_queue`, including the accepted article snapshot required for analysis.
- NewsFetcher component API for rare article rehydration or replay recovery when the queue payload is insufficient.
- Shared Instrument Registry API or explicitly documented shared read contract for active watchlist and instrument alias resolution.
- Current and historical market context from the MarketData component API when required by strategy policy.
- ThesisBuilder configuration and shared queue/database settings.

Outputs:
- `thesis_builder.t_news_analyses` rows.
- `thesis_builder.t_evidence_windows` operational aggregation state.
- `thesis_builder.t_thesis_cards` rows.
- Initial thesis-card review state through the shared review contract for cards that pass validation.
- Thesis-card signal envelopes on `signal_queue`.
- Failed envelopes on `failed_messages_dlq` after retry exhaustion.

Delivery semantics:
- Consume with at-least-once delivery.
- Acknowledge news messages only after analysis persistence and any required card/review/signal writes are complete.
- Use idempotency keys based on article id, instrument identity, strategy, and evidence set to avoid duplicate cards during retries.

## 3. Analysis Flow

For each accepted news event:

1. Resolve eligible instruments using the article snapshot plus the Shared Instrument Registry API or shared read contract.
2. Skip instruments that are not active according to the shared watchlist contract.
3. Run deterministic prechecks for article freshness, source quality, duplicate evidence, and obvious non-market relevance.
4. Optionally run the configured recall-biased triage prompt before full analysis.
5. Resolve market context through the MarketData component API when the candidate strategy requires price-derived validation.
6. Call the configured full-analysis LLM only when deterministic prechecks and enabled triage leave a plausible trading impact.
7. Persist one `t_news_analyses` row per analyzed, prefiltered, or triaged article/instrument pair.
8. Add eligible analyses to an evidence window keyed by instrument, strategy, candidate direction, and, when enabled, story identity.
9. Attempt thesis-card creation immediately after each window update.

ThesisBuilder must store analyses even when no thesis card is created, so rejected, weak, stale, or conflicting evidence remains auditable.
Analyses that classify an article as price-actionable for the analyzed instrument must set `is_market_moving=true` so monitoring can distinguish processed news from market-moving news.

### 3.0 Pair Prefilter And Triage

Deterministic pair resolution must avoid incidental URL-slug matches. Provider ticker tags create pairs, and aliases match only article headline and summary text.

Provider ticker tags are a recall channel only: they make an article/instrument pair eligible for analysis, but they are never evidence that the instrument is the article's subject. Per-symbol feeds (for example Finnhub company news) stamp the queried ticker on every returned article, including teaser articles whose headlines deliberately hide the subject company, so downstream attribution must rest on article text, not on the tag (see §3.3 subject attribution).

When the optional roundup prefilter is enabled, an article tagged with more than the configured active-instrument threshold and no headline alias match is treated as a listicle or sector roundup. ThesisBuilder persists one rejected analysis row per tagged pair with `rejection_reason_code=prefiltered_roundup` and makes no LLM call for those pairs.

When small-LLM triage is enabled, each surviving pair receives a narrow subjecthood/content-type classification before full analysis. The triage contract is recall-biased: ambiguous pairs pass through to full analysis. Clear non-subjects persist as `triage_not_subject`; clear non-catalysts or opinion/listicle content persist as `triage_not_catalyst`. Triage failures fail open to full analysis. Live processing, historical reprocess, and regeneration backtests use the same configured prefilter/triage behavior.

When `THESIS_BUILDER_STORY_SCOPING_ENABLED=true`, valid analyses run a constrained story-assignment step before evidence-window mutation. Candidates are limited to the same instrument, strategy, direction, and reprocess scope, and include collecting evidence windows plus unexpired satisfied cards with stored story narratives. The assignment output must be exactly `window:<id>`, `card:<id>`, or `new_story`. Empty candidate sets skip the LLM call and seed a new window. Invalid output or transport failure fails open to the oldest collecting window for the key, or seeds a new window when none exists, and records `assignment_source=fallback` for audit.

## 3.1 Component Boundary Rules

ThesisBuilder may directly access only the `thesis_builder` schema and explicitly documented shared contracts. It must not query `news_fetcher`, `market_data`, `trade_executor`, or any other component-owned schema.

Accepted article content must arrive in the `news_raw_queue` payload. If replay or recovery requires rehydration, ThesisBuilder must call a NewsFetcher API rather than reading `news_fetcher.t_news_articles`.

Instrument and watchlist eligibility must be resolved through the shared Instrument Registry API or an explicitly documented shared read contract. Direct references to shared physical tables are implementation details of that shared contract, not permission for ad hoc cross-schema SQL in ThesisBuilder repositories.

Review state must be written through the shared thesis-card review contract. A failed review write prevents signal publication.

## 3.2 Market Context Policy

News evidence remains the primary source for thesis-card evidence bullets. Market context is used to validate strategy fit, confidence, and risk-box construction; it does not replace the required news evidence from `docs/design/shared/product-constraint.md`.

ThesisBuilder consumes market context only through the MarketData component API:

```python
get_market_context(ticker: str, exchange_code: str, refresh_if_stale: bool = True) -> MarketContextSnapshot
```

MarketData owns cache reads, freshness evaluation, stale-context refresh, provider pacing, provider failures, and provider usage accounting. ThesisBuilder must not query MarketData tables, import MarketData storage adapters, or call market-data providers directly.

When a candidate strategy requires market context, ThesisBuilder calls `get_market_context(..., refresh_if_stale=True)`. If MarketData cannot refresh successfully or returns a snapshot with `source_status` of `stale` or `missing`, ThesisBuilder treats the context as unusable for that strategy. `fresh` and `delayed` snapshots are usable when the strategy allows delayed data.

Whenever market context affects strategy selection, confidence, or risk-box construction, ThesisBuilder persists a JSON copy of the returned snapshot on the analysis or thesis card that used it. Audit must not depend on rereading the latest MarketData cache because MarketData may refresh the context later.

ThesisBuilder also requests optional company fundamentals through the MarketData API (`get_fundamentals(...)`) and includes a small fundamentals block — market cap, shares outstanding, trailing-twelve-month revenue, and next earnings date — in the analysis prompt so the LLM can judge the materiality of an event relative to company scale (for example, a fixed-dollar contract is transformative for a small company and noise for a large one). Fundamentals are advisory prompt context only: they must never block analysis. When unavailable (no provider coverage, provider failure, or the feature disabled), the block is null and analysis proceeds unchanged. The exact fundamentals values shown to the model are persisted on the analysis row for audit, and only stable value fields (no provider/fetch timestamps) enter the prompt so that live and regeneration prompts stay byte-identical for the same underlying data.

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
- `event_driven`: market context is required at card creation for the already-priced gate. Missing, stale, or unusable context rejects the card candidate with `market_context_unavailable`.
- `sentiment_momentum`: market context is required at card creation for the already-priced gate. Missing, stale, or unusable context rejects the card candidate with `market_context_unavailable`.
- `sector_rotation`: requires enough peer, sector, or index context returned by the MarketData API to support any rotation claim.
- `contrarian_reversal`: requires usable recent and historical market context. ThesisBuilder must not create a contrarian reversal card unless price evolution shows an overextended move, stabilization or reversal evidence, and a deterministic tight risk box.
- `trend_follow`: requires usable historical market context sufficient to establish that the move is a durable trend rather than a short-lived news reaction.

When a strategy requires market context and that context is unavailable, stale, or insufficient, ThesisBuilder must fail closed for that strategy. It may still create a different strategy card from the same news evidence only if that alternate strategy satisfies its own requirements.

Already-priced gate:
- `event_driven` and `sentiment_momentum` cards are rejected when the direction-aligned 1-day return exceeds the configured return threshold or the direction-aligned price move exceeds the configured ATR-20 multiple.
- Rejected candidates are persisted as non-executable audit cards with `validation_status=rejected` and `rejection_reason_code=already_priced`.
- The triggering market-context snapshot is copied onto the card for audit.
- Thresholds are owned by ThesisBuilder configuration and surfaced read-only in Monitoring UI.

## 3.3 LLM Analysis Contract

The LLM must return structured output that ThesisBuilder can validate deterministically before persistence or card creation.

Required fields:
- `ticker`
- `exchange_code`
- `sentiment`
- `relevance`
- `urgency`
- `suggested_action`
- `candidate_strategy`
- `direction`
- `confidence`
- `reasoning`
- `is_market_moving`
- `instrument_is_subject`
- `content_type`
- `subject_relation`

Optional fields:
- `event_type`
- `event_occurred_at`
- `price_impact_magnitude`
- `impact_horizon`
- `evidence_bullet_candidates`

Allowed `candidate_strategy` values are `event_driven`, `sentiment_momentum`, `sector_rotation`, `contrarian_reversal`, and `trend_follow`. Allowed `direction` values are `buy`, `sell`, and `hold`. Allowed `subject_relation` values are `direct`, `supply_chain`, `customer_or_peer`, `macro_sector`, and `none`; `instrument_is_subject` is a compatibility field derived from `subject_relation == direct`. Invalid enum values, malformed confidence, missing required fields, or instrument mismatch must be persisted as a rejected analysis outcome and must not create an executable thesis card. Unrecognized `price_impact_magnitude` or `impact_horizon` values degrade to null (they never fail the whole analysis), which keeps historical/cached responses that predate a field parseable.

Impact-quantification fields (observe-only):
- `price_impact_magnitude` (`low`, `medium`, `high`) estimates the expected direction-aligned price move for the analyzed instrument, anchored to the instrument's own volatility rather than an absolute percentage: `low` is below 0.5x `atr_20d`, `medium` is 0.5x-1.5x, and `high` is above 1.5x. The prompt supplies this rubric together with the market-context `atr_20d` and, when available, the fundamentals block (see §3.2) so the model can weigh the event's dollar scale against company size.
- `impact_horizon` (`intraday`, `1d`, `5d`) is the window over which most of that move is expected to be realized.
- For `supply_chain` and `customer_or_peer` relations, ThesisBuilder deterministically caps `price_impact_magnitude` at `low` unless the analysis indicates a realized surprise rather than a consensus preview.
- These fields are quantified but not yet acted upon: they do not affect card expiry, risk boxes, or the published signal. Their empirical value is measured offline by the Backtester impact-calibration report (`docs/design/product_components/backtester/behavior.md` §7) before any bracket use is considered.

Event dating rules:
- `published_at` is feed publication time, not event time. Recap articles republish old events under fresh timestamps, so a freshness measure keyed to `published_at` alone misreads them as breaking news — and the already-priced gate simultaneously misreads them as un-priced, because the settled move no longer shows in the 1-day return.
- The full-analysis prompt requires the model to return `event_occurred_at`: the ISO 8601 date (or datetime, when stated) on which the reported event actually occurred or was announced, extracted from the headline/summary text (e.g. "On July 9, Micron ... said"), resolving relative expressions ("last week") against `published_at`. When the text does not date the event, the field is null and freshness falls back to `published_at`. The triage prompt does not extract it; deterministic enforcement happens at window/card level.
- An absent or unparseable `event_occurred_at` degrades to null and never fails the whole analysis, keeping historical/cached responses that predate the field parseable.
- Each analyzed article's **effective evidence timestamp** is `min(published_at, event_occurred_at)` when the event date is present, else `published_at`. The evidence-window retention cutoff (§4) and the card freshness gate (§5) measure age from this timestamp.

**Implementation status:** implemented by issue 260715-03. ThesisBuilder extracts and persists `event_occurred_at` from full analysis responses, degrades absent or malformed values to null, and applies effective evidence timestamps to retention and card freshness gates.

Subject attribution rules:
- The analysis and triage prompts present provider ticker tags as feed provenance (`feed_tags` — which feed returned the article), never as ground-truth attribution, and instruct the model that `subject_relation=direct` requires the company, its products, or its ticker to be explicitly named in the article headline or summary.
- The `direct` label is not taken from the LLM on trust. After the LLM call, ThesisBuilder deterministically verifies that the instrument is named in the article headline or summary (matched against ticker, instrument aliases, and display name — the same matching used for pair resolution). An unverified `direct` is downgraded to `customer_or_peer` and the downgrade is recorded for audit; the downgraded analysis inherits the full indirect policy (anchor-evidence requirement, low-magnitude cap absent a realized surprise, ineligibility as a card seed).
- Because unverified directs are downgraded before evidence-window mutation, anchor evidence and card seeds are always text-verified: a set of articles none of which names the instrument can never form a thesis card, regardless of provider tags.
- Live processing, historical reprocess, and regeneration backtests apply identical prompt content and verification so sim funnels remain comparable to production.

## 4. Evidence Aggregation

ThesisBuilder aggregates only until enough evidence exists to make a trade decision.

When story scoping is enabled, each new window stores a seed-stable `story_narrative` derived from the seeding article headline, analysis `event_type`, and `evidence_bullet_candidates`. The seed narrative is not rewritten by later matched articles, so story identity cannot drift as coverage accumulates. Multiple collecting windows may exist for the same instrument, strategy, and direction; the assignment result, not a database uniqueness constraint, selects the target window.

Window satisfaction rules:
- The window must meet all evidence rules from `docs/design/shared/product-constraint.md`.
- The default required evidence count is read from `THESIS_CARD_REQUIRED_EVIDENCE_COUNT`.
- Evidence must support one coherent instrument, direction, strategy, and time horizon.
- Indirect evidence (`supply_chain` or `customer_or_peer`) is supplementary only. It is valid only when the assigned story target already has an active thesis card or direct evidence in the assigned collecting window. If story assignment resolves to `new_story`, indirect evidence is rejected with `indirect_no_anchor_evidence` and no new window is created. With story scoping disabled, the same rule falls back to the legacy instrument/strategy/direction window key. A window containing only indirect evidence never satisfies the seed requirement.
- Conflicting high-confidence evidence prevents card creation until the conflict is resolved by newer or stronger evidence.

Window terminal states:
- `satisfied`: a valid thesis card was created.
- `rejected`: evidence is structurally invalid, contradictory, below confidence, or otherwise non-actionable.
- `expired`: legacy state from the anchored-window design; no longer produced (see below), retained only for pre-existing rows.

The collection span is rolling, not anchored to the first article. On each new eligible analysis, evidence whose effective evidence timestamp (§3.3) is older than `THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES` (default 1000) relative to the analysis time ages out of the window individually; the window itself stays `collecting` and `window_started_at` tracks the oldest retained article. This guarantees a new arrival always lands in live collecting state (it is never discarded into a window that expired underneath it) and that evidence clusters straddling an arbitrary first-article anchor still form cards. The span is a ceiling, not a delay target: if sufficient evidence arrives earlier, ThesisBuilder creates the card immediately. Card-level freshness is enforced separately by `THESIS_CARD_MAX_EVIDENCE_AGE_MINUTES`.

If story assignment targets an unexpired satisfied card, the incoming article is recorded as card corroboration and the existing card remains frozen: evidence, confidence, validation status, shared review state, and signal publication are not modified.

## 5. Thesis-Card Creation

The default time horizon is `swing_1d_5d`.

ThesisBuilder may emit longer trend-following cards when the evidence indicates a durable trend rather than a short-lived news reaction. Longer trend-follow cards must:
- Use a distinct strategy value such as `trend_follow`.
- Set an explicit time horizon longer than `swing_1d_5d`, capped by `THESIS_BUILDER_TREND_FOLLOW_MAX_DAYS`.
- Include an invalidation condition that explains what would end the trend thesis.
- Remain subject to the same evidence and approval constraints as all other cards.

Card creation steps:
1. Select the best evidence set from the satisfied window.
2. Measure evidence freshness from each article's effective evidence timestamp (§3.3) to the card validation decision time.
3. Apply deterministic gates such as freshness and already-priced suppression.
4. If card synthesis is enabled, send the full evidence dossier and market-context snapshot to the
   configured synthesis model for an `approve` / `reject` verdict. Reject, malformed output, or
   synthesis unavailability fails closed by default and creates no executable card; the verdict is
   persisted for audit. The explicit fallback flag may restore the mechanical assembly path.
5. Generate exactly the canonical card fields required by `docs/design/shared/product-constraint.md`.
   With synthesis enabled and approved, confidence, evidence bullets, summary, and risk text come
   from the synthesis output. With synthesis disabled, these fields are assembled mechanically from
   per-article analyses as before.
6. Generate ThesisBuilder-owned initial risk fields: max loss, stop condition, and invalidation condition.
7. Validate the card deterministically.
8. Persist the thesis card, copying the evidence window `story_narrative` onto the card when story scoping is enabled.
9. Write the initial shared review state.
10. Publish the card signal only if validation passes and review state is approved.

Initially, all valid cards are preapproved by system policy. ThesisBuilder writes `shared.t_thesis_card_reviews.decision_state=approved`, `reviewed_by=system_policy`, and a review reason identifying the ThesisBuilder policy version. If `THESIS_BUILDER_INITIAL_REVIEW_POLICY=manual` is enabled later, valid cards are persisted but not published as executable signals until a UI/user approval exists.
The physical review state is owned by the shared contract; ThesisBuilder code should use the shared review API/adapter instead of ad hoc SQL against shared tables.

Freshness policy:
- The maximum allowed age for evidence used in an executable thesis card is `THESIS_CARD_MAX_EVIDENCE_AGE_MINUTES`, defaulting to 1400 minutes. The same limit applies to article age and event age; there is no separate event-age knob.
- `max_evidence_age_seconds` is the oldest evidence age at validation time, measured from each article's effective evidence timestamp (§3.3).
- `allowed_max_evidence_age_seconds` is the configured freshness limit.
- `evidence_age_exceeded_seconds` is `max(0, max_evidence_age_seconds - allowed_max_evidence_age_seconds)`.
- If evidence would otherwise create a thesis card but exceeds the freshness limit, ThesisBuilder persists a non-executable audit card with `validation_status=rejected`. The reason code is `stale_event` when the violation exists only because of an article's `event_occurred_at` (its `published_at` age alone would have passed) — i.e. a recap of an old event — and `stale_evidence` otherwise. The operator-facing UI label for both is `stale`.
- Stale audit cards must not receive shared review rows and must not be published to `signal_queue`.

### 5.1 Regime Posture (informative)

The default `swing_1d_5d` horizon and the evidence/freshness requirements above
target multi-day, news-confirmed theses — not sub-minute breaking-news spikes.
This is intentional: ingestion cadence, the multi-source confirmation rule, and
the LLM analysis step put a hard floor on reaction time, so the initial jump on
clean fast news is structurally unreachable. ThesisBuilder should fail closed on
already-priced moves rather than chase them. See "Target Regime & Latency
Posture" in `docs/design/overview.md` §1.4 for the full rationale.

**Implementation status:** already-priced suppression is enforced for
`event_driven` and `sentiment_momentum` cards. ThesisBuilder rejects a card
candidate when market context shows the direction-aligned 1-day move exceeds the
configured return threshold or ATR-20 multiple. Missing or stale market context
fails closed with `market_context_unavailable`. The LLM prompt also includes an
advisory instruction to lower confidence or prefer `hold` when market context
shows the move is already realized; the deterministic gate remains authoritative.
Note the gate measures the *recent* move at analysis time: an event old enough
that its move has settled out of the 1-day return passes this gate while being
maximally priced-in. That blind spot is closed by the event-age freshness gate
(`stale_event`, §5 and 260715-03), not by widening the return window here.

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
- Missing shared review contract write prevents signal publication.
- LLM timeout or provider failure retries according to shared queue retry policy, then dead-letters the message.
- ThesisBuilder persistence failure or required shared-contract write failure prevents ACK and must not publish a signal.
- Signal publish failure leaves the message unacknowledged or writes a dead-letter according to retry state.

Failures should include concise machine-readable error codes and enough context to inspect the affected article, instrument, and evidence window.

## 10. Source Organization

Default implementation placement:
- Process entry point: `src/product_components/thesis_builder`.
- ThesisBuilder persistence SQL: `src/product_components/thesis_builder/db/schema.sql`.
- ThesisBuilder repositories may access only ThesisBuilder-owned tables. External component data must be accessed through API or event adapters.
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
- Component-boundary checks that ThesisBuilder repositories do not reference foreign component schemas.
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
