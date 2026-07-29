# ThesisBuilder Configuration

Configuration owned by the ThesisBuilder process.

## Environment Variables

```bash
# Database ownership
THESIS_BUILDER_DB_SCHEMA=thesis_builder

# LLM provider
OPENAI_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_DAILY_TOKEN_BUDGET=5000000
THESIS_BUILDER_LLM_MAX_OUTPUT_TOKENS=1200
THESIS_BUILDER_TRIAGE_ENABLED=false
THESIS_BUILDER_TRIAGE_MODEL=gpt-4o-mini
THESIS_BUILDER_TRIAGE_MAX_OUTPUT_TOKENS=200
THESIS_BUILDER_STORY_SCOPING_ENABLED=false
THESIS_BUILDER_STORY_ASSIGNMENT_MODEL=gpt-4o-mini
THESIS_BUILDER_STORY_ASSIGNMENT_MAX_OUTPUT_TOKENS=120
THESIS_BUILDER_SYNTHESIS_ENABLED=false
THESIS_BUILDER_SYNTHESIS_MODEL=gpt-4o-mini
THESIS_BUILDER_SYNTHESIS_MAX_OUTPUT_TOKENS=1200
THESIS_BUILDER_SYNTHESIS_FALLBACK_TO_MECHANICAL=false
LLM_FALLBACK_PROVIDER=groq   # "groq" | "gemini" | "none"

# Thesis-card policy
THESIS_BUILDER_DEFAULT_TIME_HORIZON=swing_1d_5d
THESIS_BUILDER_ENABLE_TREND_FOLLOW_EXTENSION=true
THESIS_BUILDER_TREND_FOLLOW_MAX_DAYS=20
THESIS_BUILDER_INITIAL_REVIEW_POLICY=preapproved   # "preapproved" | "manual"
THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES=1000
THESIS_CARD_MAX_EVIDENCE_AGE_MINUTES=1400
THESIS_CARD_REQUIRED_EVIDENCE_COUNT=3
THESIS_BUILDER_MIN_CONFIDENCE=0.6
THESIS_BUILDER_CONTRARIAN_MIN_CONFIDENCE=0.72
THESIS_BUILDER_TREND_FOLLOW_MIN_CONFIDENCE=0.68
THESIS_BUILDER_RISK_MAX_LOSS_USD=150
THESIS_BUILDER_TRADEABILITY_MAX_ENTRY_PRICE=1000
THESIS_BUILDER_TRADEABILITY_ATR_STOP_MULT=1.5
THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED=false
THESIS_BUILDER_LISTICLE_PREFILTER_TAG_THRESHOLD=6
THESIS_BUILDER_ALREADY_PRICED_EVENT_DRIVEN_ATR_MULTIPLE=1.5
THESIS_BUILDER_ALREADY_PRICED_EVENT_DRIVEN_RETURN_THRESHOLD=0.04
THESIS_BUILDER_ALREADY_PRICED_SENTIMENT_MOMENTUM_ATR_MULTIPLE=2.0
THESIS_BUILDER_ALREADY_PRICED_SENTIMENT_MOMENTUM_RETURN_THRESHOLD=0.06

# Pipeline scheduling
PIPELINE_INTERVAL=120        # seconds
THESIS_BUILDER_CONSUMER_GROUP=thesis_builder_group

# Historical reprocess (operator-triggered via Monitoring UI)
REPROCESS_COMMAND_QUEUE=reprocess_command_queue
THESIS_BUILDER_TAXONOMY_COMMAND_QUEUE=taxonomy_command_queue
THESIS_BUILDER_TAXONOMY_BACKFILL_BATCH_SIZE=100
THESIS_BUILDER_REPROCESS_MAX_ARTICLES=200
```

## Historical Reprocess

ThesisBuilder owns the historical reprocess workflow. Operators trigger a run from the Monitoring UI, which enqueues a command on `REPROCESS_COMMAND_QUEUE` and records an `accepted` row in `thesis_builder.t_reprocess_runs`. The ThesisBuilder runtime consumes the command and executes the reprocess in a background thread, so the live news consumer loop is never paused. A partial unique index (`uq_reprocess_runs_active`) enforces at most one `accepted`/`running` run at a time. Run status and result counts are read back through the ThesisBuilder-owned reprocess gateway; the LLM model and reprocess policy come from ThesisBuilder settings, not from the caller.

Taxonomy decisions use a separate ThesisBuilder-owned command stream. The runtime
also recovers accepted commands from PostgreSQL, so a transient Redis publish
failure does not lose an already accepted command. Historical reclassification
runs one bounded batch per runtime cycle; the batch size must remain between 1
and 1000.

## Pair Prefilter And Triage

ThesisBuilder first resolves article/instrument pairs deterministically. Alias matching uses headline and summary text; URL slugs are not used for alias matches because they can create incidental subject matches. Provider ticker tags still create pairs.

`THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED` enables a conservative roundup heuristic. When enabled, an article tagged with more than `THESIS_BUILDER_LISTICLE_PREFILTER_TAG_THRESHOLD` active watchlist instruments and no headline alias match is persisted as rejected analyses with `rejection_reason_code=prefiltered_roundup` without an LLM call. The default is disabled.

`THESIS_BUILDER_TRIAGE_ENABLED` enables a recall-biased small-LLM triage call before full analysis. Triage returns only subjecthood and `content_type`; clear non-subjects persist as `triage_not_subject`, clear non-catalysts persist as `triage_not_catalyst`, and ambiguous cases pass through to the full analysis prompt. Triage tokens share the same ThesisBuilder run budget as full analysis and are stored on the rejected analysis rows when triage rejects a pair. The default is disabled until replay validation shows acceptable recall.

`THESIS_BUILDER_STORY_SCOPING_ENABLED` enables story-aware evidence grouping after a valid analysis is persisted. When disabled, ThesisBuilder preserves the legacy single collecting window per instrument/strategy/direction behavior and makes no assignment calls. When enabled, the assignment call uses `THESIS_BUILDER_STORY_ASSIGNMENT_MODEL` and `THESIS_BUILDER_STORY_ASSIGNMENT_MAX_OUTPUT_TOKENS` to choose `window:<id>`, `card:<id>`, or `new_story` from same-key candidates. Assignment tokens count against the ThesisBuilder LLM budget and assignment decisions are persisted in `thesis_builder.t_story_assignments`.

## Card Synthesis

`THESIS_BUILDER_SYNTHESIS_ENABLED` enables a second LLM pass when an evidence window satisfies
deterministic card-creation gates. The synthesis prompt receives the selected evidence, market
context snapshot, candidate strategy/direction/horizon, and mechanical risk box. It returns an
`approve` or `reject` verdict plus synthesis confidence, selected evidence bullets, and risk text.

The default is disabled, preserving the pre-synthesis mechanical assembly path. When enabled,
synthesis failure or malformed output fails closed by default: the evidence window is marked
`rejected` with `synthesis_unavailable` or `synthesis_invalid` and no executable card is created.
`THESIS_BUILDER_SYNTHESIS_FALLBACK_TO_MECHANICAL=true` is an explicit operator override that falls
back to the old mechanical assembly if synthesis is unavailable.

Synthesis uses `THESIS_BUILDER_SYNTHESIS_MODEL` and
`THESIS_BUILDER_SYNTHESIS_MAX_OUTPUT_TOKENS`; tokens count against the ThesisBuilder LLM budget.
Synthesis verdicts are persisted in `thesis_builder.t_card_synthesis_verdicts` for audit.

## Confidence Thresholds

`THESIS_BUILDER_MIN_CONFIDENCE`, `THESIS_BUILDER_CONTRARIAN_MIN_CONFIDENCE`, and
`THESIS_BUILDER_TREND_FOLLOW_MIN_CONFIDENCE` are syntactic admission thresholds over the LLM's
self-reported confidence. They are not currently a calibrated safety signal: recent regeneration
runs showed reported confidence clustering above the default 0.6 floor. Keep these thresholds
documented and measurable, but do not treat 0.6 as a proven risk-control boundary until the
Backtester confidence-calibration report has enough closed trades to show discrimination. If the
report remains non-discriminative at the holdout sample size, confidence-derived gating and
risk-box scaling should be neutralized rather than tuned by intuition.

## Already-Priced Gate

At thesis-card creation time, ThesisBuilder rejects `event_driven` and `sentiment_momentum` cards when the market-context snapshot shows the instrument has already moved too far in the thesis direction. The gate checks both 1-day direction-aligned return and direction-aligned price move measured in ATR-20 units. Exceeding either configured threshold persists the candidate as a rejected audit card with `rejection_reason_code=already_priced`; missing or stale context persists `market_context_unavailable` for gated strategies. The same thresholds are used by live processing, historical reprocess, and regeneration backtests.

Thresholds are ThesisBuilder-owned configuration. Monitoring UI exposes them read-only in the ThesisBuilder tab.

## Tradeability Gate

At thesis-card creation time, ThesisBuilder rejects otherwise-valid card candidates that cannot size
at least one integer share under TradeExecutor's live sizing rules. The gate fails closed when the
market-context snapshot is missing, stale, or lacks `current_price` / `atr_20d`. It rejects with
`untradeable_risk_box` when `current_price > THESIS_BUILDER_TRADEABILITY_MAX_ENTRY_PRICE` or
`THESIS_BUILDER_TRADEABILITY_ATR_STOP_MULT * atr_20d > THESIS_BUILDER_RISK_MAX_LOSS_USD`.

`THESIS_BUILDER_TRADEABILITY_MAX_ENTRY_PRICE` must stay aligned with TradeExecutor's
`MAX_POSITION_SIZE`, and `THESIS_BUILDER_TRADEABILITY_ATR_STOP_MULT` must stay aligned with
TradeExecutor's `ATR_STOP_MULT`. If TradeExecutor adds fractional-share support or intentionally
raises its notional/risk caps, update these ThesisBuilder thresholds in the same change.

## Shared Dependencies

ThesisBuilder also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.

## MarketData Dependency

ThesisBuilder depends on the MarketData component API for strategy-required market context. MarketData owns quote/bar freshness settings, delayed-data policy, provider pacing, and stale-context refresh behavior. ThesisBuilder consumes the returned `source_status` and copied context snapshot; it does not define separate market-data freshness environment variables.
