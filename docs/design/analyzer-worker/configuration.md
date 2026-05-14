# AnalyzerWorker Configuration

Configuration owned by the AnalyzerWorker process.

## Environment Variables

```bash
# Database ownership
ANALYZER_DB_SCHEMA=analyzer_worker

# LLM provider
OPENAI_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_DAILY_TOKEN_BUDGET=500000
LLM_FALLBACK_PROVIDER=groq   # "groq" | "gemini" | "none"

# Pipeline scheduling
PIPELINE_INTERVAL=120        # seconds
```

## Shared Dependencies

AnalyzerWorker also depends on shared PostgreSQL connection, operational, and queue settings defined in `docs/design/shared/configuration.md`.
