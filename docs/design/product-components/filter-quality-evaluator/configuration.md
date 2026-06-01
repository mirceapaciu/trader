# Filter Quality Evaluator Configuration

Configuration owned by the Filter Quality Evaluator process.

## Environment Variables

```bash
# Database ownership
FILTER_QUALITY_DB_SCHEMA=filter_quality_evaluator

# Run execution mode
FILTER_QUALITY_DEFAULT_TRIGGER_MODE=manual_cli
FILTER_QUALITY_MAX_ITEMS_PER_RUN=1000
FILTER_QUALITY_BATCH_SIZE=50

# Dataset defaults
FILTER_QUALITY_DEFAULT_LOOKBACK_HOURS=24
FILTER_QUALITY_REQUIRE_TIME_WINDOW=true
FILTER_QUALITY_ALLOW_CONFIG_FINGERPRINT_FILTER=true
FILTER_QUALITY_ACCEPTED_AUDIT_ENABLED=false
FILTER_QUALITY_ACCEPTED_AUDIT_SAMPLE_SIZE=200

# LLM evaluation policy
FILTER_QUALITY_LLM_MODEL=gpt-4o-mini
FILTER_QUALITY_LLM_MAX_TOKENS_PER_RUN=200000
FILTER_QUALITY_LLM_MAX_TOKENS_PER_ITEM=1500
FILTER_QUALITY_MIN_CONFIDENCE_THRESHOLD=0.60

# Accepted-audit cost estimation
FILTER_QUALITY_TRADING_LLM_AVG_TOKENS_PER_ITEM=1200
FILTER_QUALITY_TRADING_LLM_COST_PER_1K_TOKENS=0.010

# Output policy
FILTER_QUALITY_INCLUDE_ITEM_RATIONALE=true
FILTER_QUALITY_INCLUDE_IMPROVEMENT_SUGGESTION=true

# Operations
FILTER_QUALITY_RUN_TIMEOUT_SECONDS=1800
FILTER_QUALITY_LOG_LEVEL=INFO
```

## Shared Dependencies

Filter Quality Evaluator also depends on shared PostgreSQL connection and operational settings defined in `docs/design/shared/configuration.md`.

## Ownership Rules

- Variables prefixed with `FILTER_QUALITY_` are owned by Filter Quality Evaluator.
- NewsFetcher variables remain owned by `docs/design/product-components/news-fetcher/configuration.md`.
- Shared cross-process variables belong in `docs/design/shared/configuration.md`.
