# ThesisBuilder Configuration

Configuration owned by the ThesisBuilder process.

## Environment Variables

```bash
# Database ownership
THESIS_BUILDER_DB_SCHEMA=thesis_builder

# LLM provider
OPENAI_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_DAILY_TOKEN_BUDGET=500000
LLM_FALLBACK_PROVIDER=groq   # "groq" | "gemini" | "none"

# Thesis-card policy
THESIS_BUILDER_DEFAULT_TIME_HORIZON=swing_1d_5d
THESIS_BUILDER_ENABLE_TREND_FOLLOW_EXTENSION=true
THESIS_BUILDER_TREND_FOLLOW_MAX_DAYS=20
THESIS_BUILDER_INITIAL_REVIEW_POLICY=preapproved   # "preapproved" | "manual"
THESIS_BUILDER_EVIDENCE_COLLECTION_MAX_MINUTES=120
THESIS_CARD_REQUIRED_EVIDENCE_COUNT=3
THESIS_BUILDER_MIN_CONFIDENCE=0.6
THESIS_BUILDER_CONTRARIAN_MIN_CONFIDENCE=0.72
THESIS_BUILDER_TREND_FOLLOW_MIN_CONFIDENCE=0.68
THESIS_BUILDER_RISK_MAX_LOSS_USD=120

# Pipeline scheduling
PIPELINE_INTERVAL=120        # seconds
```

## Shared Dependencies

ThesisBuilder also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.

## MarketData Dependency

ThesisBuilder depends on the MarketData component API for strategy-required market context. MarketData owns quote/bar freshness settings, delayed-data policy, provider pacing, and stale-context refresh behavior. ThesisBuilder consumes the returned `source_status` and copied context snapshot; it does not define separate market-data freshness environment variables.
