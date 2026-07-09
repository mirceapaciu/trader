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
THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED=false
THESIS_BUILDER_LISTICLE_PREFILTER_TAG_THRESHOLD=6

# Pipeline scheduling
PIPELINE_INTERVAL=120        # seconds
THESIS_BUILDER_CONSUMER_GROUP=thesis_builder_group

# Historical reprocess (operator-triggered via Monitoring UI)
REPROCESS_COMMAND_QUEUE=reprocess_command_queue
THESIS_BUILDER_REPROCESS_MAX_ARTICLES=200
```

## Historical Reprocess

ThesisBuilder owns the historical reprocess workflow. Operators trigger a run from the Monitoring UI, which enqueues a command on `REPROCESS_COMMAND_QUEUE` and records an `accepted` row in `thesis_builder.t_reprocess_runs`. The ThesisBuilder runtime consumes the command and executes the reprocess in a background thread, so the live news consumer loop is never paused. A partial unique index (`uq_reprocess_runs_active`) enforces at most one `accepted`/`running` run at a time. Run status and result counts are read back through the ThesisBuilder-owned reprocess gateway; the LLM model and reprocess policy come from ThesisBuilder settings, not from the caller.

## Pair Prefilter And Triage

ThesisBuilder first resolves article/instrument pairs deterministically. Alias matching uses headline and summary text; URL slugs are not used for alias matches because they can create incidental subject matches. Provider ticker tags still create pairs.

`THESIS_BUILDER_LISTICLE_PREFILTER_ENABLED` enables a conservative roundup heuristic. When enabled, an article tagged with more than `THESIS_BUILDER_LISTICLE_PREFILTER_TAG_THRESHOLD` active watchlist instruments and no headline alias match is persisted as rejected analyses with `rejection_reason_code=prefiltered_roundup` without an LLM call. The default is disabled.

`THESIS_BUILDER_TRIAGE_ENABLED` enables a recall-biased small-LLM triage call before full analysis. Triage returns only subjecthood and `content_type`; clear non-subjects persist as `triage_not_subject`, clear non-catalysts persist as `triage_not_catalyst`, and ambiguous cases pass through to the full analysis prompt. Triage tokens share the same ThesisBuilder run budget as full analysis and are stored on the rejected analysis rows when triage rejects a pair. The default is disabled until replay validation shows acceptable recall.

## Shared Dependencies

ThesisBuilder also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.

## MarketData Dependency

ThesisBuilder depends on the MarketData component API for strategy-required market context. MarketData owns quote/bar freshness settings, delayed-data policy, provider pacing, and stale-context refresh behavior. ThesisBuilder consumes the returned `source_status` and copied context snapshot; it does not define separate market-data freshness environment variables.
