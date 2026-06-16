# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read and follow all instructions in AGENTS.md. The sections below supplement those instructions with additional context useful for navigating the codebase.

## Commands

### Python (backend)

All Python commands use `uv`:

```bash
uv sync                                           # install dependencies
uv run pytest                                     # all unit tests
uv run pytest tests/path/to/test_file.py          # single file
uv run pytest tests/path/to/test_file.py::test_fn # single test
uv run pytest -m "not integration"                # unit tests only
uv run pytest -m integration                      # integration tests (requires live Postgres + Redis)

# Run components directly
uv run python -m src.product_components.news_fetcher
uv run python -m src.product_components.thesis_builder
uv run python -m src.product_components.monitoring_ui.backend
```

### Frontend (monitoring UI)

```bash
cd src/product_components/monitoring_ui/frontend
npm install
npm run dev     # Vite dev server
npm test        # Vitest unit tests
npm run build   # TypeScript check + Vite build
```

### Infrastructure

```bash
# Start/stop Docker containers
scripts/deployment/postgres/start.sh
scripts/deployment/redis/start.sh

# Start components as background processes (PID and logs written to logs/)
scripts/deployment/news-fetcher/start.sh
scripts/deployment/thesis-builder/start.sh
scripts/deployment/monitoring-ui/start.sh [backend_port] [frontend_port]  # defaults: 8090, 5174
scripts/deployment/monitoring-ui/stop.sh
```

## Architecture

### Data flow

```
News APIs (Finnhub, Marketaux) + RSS
        |
    NewsFetcher  (polling loop)
        |  filter -> dedupe -> persist -> publish obligation
        v
    Redis Stream: news_raw_queue
        |
    ThesisBuilder  (stream consumer group)
        |  LLM analysis per instrument, accumulates evidence into thesis cards
        v
    Redis Stream: signal_queue  +  Postgres: thesis cards
        |
    TradeExecutor  (DB schema exists; not yet active)
```

### Source layout

```
src/
  core_components/
    event_ingestion_engine/     # Reusable: fetch -> canonicalize -> dedupe -> persist -> publish -> checkpoint
  product_components/
    shared/
      adapters.py               # Postgres implementations of shared contracts
      instrument_lookup.py      # Ticker lookup (MassiveFinance + AlphaVantage providers)
      db/schema.sql             # Shared tables: instruments, watchlist, aliases, api_usage, thesis_card_reviews, lookup_cache
    news_fetcher/               # Polling service; providers: Finnhub, Marketaux, RSS
    market_data/                # Price/quote cache used by ThesisBuilder for context (IB provider)
    thesis_builder/             # Redis consumer: OpenAI LLM thesis card generation
    filter_quality_evaluator/   # Offline tool: replay historical articles against a filter config
    monitoring_ui/
      backend/                  # FastAPI app (default port 8090)
      frontend/                 # React 19 + Vite + TanStack Query + Recharts
    trade_executor/             # DB schema only; not yet active
tests/
  core_components/              # Unit tests for EventIngestionEngine
  product_components/           # Unit tests per component (no external deps)
  integration/                  # Live Postgres + Redis; reads .env.shared + .env.test
```

### Key patterns

**EventIngestionEngine** (`src/core_components/event_ingestion_engine/engine.py`): reused by NewsFetcher. Enforces persist-before-publish, tracks `PublicationObligation` per event (PENDING -> PUBLISHING -> PUBLISHED | DEAD_LETTERED), advances checkpoint only when all obligations are terminal.

**Settings**: every component has a `Settings.from_env()` frozen dataclass. Env files layer as: `.env.shared` -> `.env.prod` -> `.env.<component>` -> `.env.secrets`. Real secrets go in `.env.secrets` (gitignored).

**Shared contracts**: components must not query other components' schemas directly. Cross-cutting access goes through `src/product_components/shared/adapters.py`: `PostgresSharedInstrumentRegistry`, `PostgresSharedInstrumentAdmin`, `PostgresSharedApiUsageWriter`, `PostgresSharedThesisCardReviewWriter`.

**DB bootstrapping**: each component runs its own `db/schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS`) on startup, plus the shared schema SQL.

**`pythonpath = ["."]`** in `pyproject.toml` means all imports start from repo root: `from src.product_components.xxx import ...`.

### Environment files

| File | Purpose |
|------|---------|
| `.env.shared` | Shared defaults (DB, Redis, queue names) |
| `.env.prod` | Production overrides |
| `.env.news-fetcher` | NewsFetcher settings |
| `.env.thesis-builder` | ThesisBuilder settings |
| `.env.monitoring-ui` | MonitoringUI settings |
| `.env.secrets` | API keys (Finnhub, Marketaux, OpenAI, AlphaVantage) — gitignored |
| `.env.test` | Integration test overrides |
