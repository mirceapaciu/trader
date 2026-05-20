# News-Based Trading Bot — System Design

## Documentation Map

This documentation is split into a system overview and implementation specs:

- This file (`docs/design/overview.md`): system goals, architecture, component boundaries, and lifecycle.
- Core technology specification: `docs/design/core-components/event-ingestion-engine/event-ingestion-engine.md`.
- Data model index and component-owned schema specs: `docs/design/data-model.md`.
- Configuration index and component-owned settings: `docs/design/configuration.md`.
- Product identity constraint and thesis-card contract: `docs/design/shared/product-constraint.md`.
- Deployment specs: `docs/design/deployment/`.
- Component folders (owner-based): `docs/design/product-components/news-fetcher/`, `docs/design/product-components/analyzer-worker/`, `docs/design/product-components/trade-executor/`, `docs/design/shared/`.

Implementation code and coding agents should follow this map and avoid adding implementation-level detail directly in this overview unless it changes architecture-level behavior.

## 1. Overview

An automated trading bot for a small private trader that:

1. Fetches financial news from real-time feeds
2. Analyzes news sentiment and relevance using an LLM
3. Generates trading decisions (buy / sell / hold)
4. Executes trades via the Interactive Brokers (IBKR) API
5. Provides a UI for monitoring and manual overrides

**Target monthly budget: ≤ 100 EUR** for all external API costs.

### 1.1 Design Principle: The Core Tech Must Be Separable From The Product

This project must develop and maintain reusable core technology that supports the trading bot but is not the trading bot itself.

Core tech can be a method, library, workflow component, or tooling capability that remains valuable even if product direction changes.

Why this is mandatory:
- Product features may pivot, but core technology should compound over time.
- Reusable technical assets create long-term leverage and reduce rework.
- Architecture and implementation choices should favor extraction into reusable IP where practical.

How this principle applies in this project:
- Design services and modules to be reusable outside this specific UI/workflow.
- Keep domain logic separable from product-specific orchestration and presentation.
- Prefer stable, generic interfaces for components that may be reused by future products.
- During feature design and coding, reject solutions that only solve one product path without contributing reusable capability.

### 1.2 Named Core Asset

This project's named core asset is the **Real-time Event Ingestion and Signal Preprocessing Engine**.

The core-technology definition, interfaces, portability constraints, and non-trading reuse scenarios are specified in `docs/design/core-components/event-ingestion-engine/event-ingestion-engine.md`.

### 1.3 Product Identity Constraint (Mandatory)

The product-wide constraint is **No Thesis Card, No Trade**.

Every trade candidate must be represented as one thesis card with a fixed shape and a deterministic validation policy before execution can be attempted. This constraint is mandatory across UI, analyzer, and trade execution flows.

The canonical contract, validation rules, and acceptance criteria are defined in `docs/design/shared/product-constraint.md`.

---

## 2. Budget Plan

| Service                      | Provider         | Tier          | Monthly Cost |
|------------------------------|------------------|---------------|-------------|
| Financial news feed          | Finnhub          | Free          | $0          |
| News enrichment (headlines)  | Marketaux        | Free / Basic  | $0 – $19    |
| Supplementary news           | RSS feeds        | Free          | $0          |
| LLM inference                | OpenAI (gpt-4o-mini) | Pay-as-you-go | ~$5 – $15 |
| IBKR market data             | Interactive Brokers | US Equities | ~$4.50      |
| IBKR API                     | Interactive Brokers | Included    | $0          |
| **Total**                    |                  |               | **~$10 – $39** |

### Budget News Feed Options

| Provider      | Free Tier                          | Paid Tier              | Why Consider                                   |
|---------------|------------------------------------|------------------------|-------------------------------------------------|
| **Finnhub**   | 60 calls/min, real-time news       | $49.99/mo (premium)    | Generous free tier; company news + sentiment    |
| **Marketaux** | 100 req/day, 3-day history         | $19/mo (500 req/day)   | Structured news with entities & sentiment       |
| **EODHD**     | 20 req/day                         | $19.99/mo              | News + fundamentals + EOD data bundle           |
| **Polygon.io**| 5 calls/min, delayed               | $29/mo (Starter)       | High quality; includes ticker news              |
| **RSS feeds** | Unlimited (Reuters, Yahoo, SeekingAlpha) | Free          | Zero cost; needs custom parsing                 |
| **Benzinga Pro**  | —                              | $37/mo (Basic)         | Full newsfeed included; no advanced filtering   |

**Recommended combination (free tier):**
- **Finnhub** as the primary news source (real-time company news, 60 calls/min)
- **RSS feeds** from Yahoo Finance, Reuters, and MarketWatch as supplementary sources
- **Marketaux** free tier for structured entity-tagged news when Finnhub quota is exhausted

**If budget allows ($19–$29/mo for news):**
- Upgrade Marketaux to Basic ($19/mo) for 500 requests/day and 1-year history
- Or use Polygon.io Starter ($29/mo) for real-time ticker news with high data quality

**If budget allows (~$37/mo for premium news):**
- Benzinga Pro Basic can be used for fast full-newsfeed coverage, but advanced filtering requires higher tiers

### LLM Provider Options

| Provider             | Model          | Cost                      | Notes                            |
|----------------------|----------------|---------------------------|----------------------------------|
| **OpenAI**           | gpt-4o-mini    | $0.15 / 1M input tokens   | Best quality-to-cost ratio       |
| **Groq**             | llama-3.1-8b   | Free (rate-limited)        | Fast; good for backup            |
| **Google**           | Gemini Flash   | Free tier: 15 req/min      | Good free option                 |
| **Local (Ollama)**   | llama-3.1-8b   | $0 (needs GPU)            | No API costs; needs hardware     |

**Recommended:** OpenAI gpt-4o-mini as primary. At ~200 news items/day with ~500 tokens per analysis, monthly cost ≈ $3–$10.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────┐
│                     UI Layer                        │
│              src/ui (monitoring dashboard)          │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│              Process-Based Service Layer            │
│                                                     │
│  [Process A] NewsFetcher                            │
│  [Process B] AnalyzerWorker                         │
│  [Process C] TradeExecutor                          │
└──────────────────────┬──────────────────────────────┘
                       │ publish / consume
┌──────────────────────▼──────────────────────────────┐
│         Message Broker (standalone process)         │
│                                                     │
│  news_raw_queue    (NewsFetcher → AnalyzerWorker)   │
│  signal_queue      (AnalyzerWorker → TradeExecutor) │
│  failed_messages_dlq  (dead-letter)                 │
│                                                     │
│  Backend: Redis Streams (default) or RabbitMQ       │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│              Repository Layer                       │
│  ┌──────────────┐ ┌─────────────┐ ┌──────────────┐  │
│  │ NewsRepo     │ │ TradeRepo   │ │ DecisionRepo │  │
│  │ (PostgreSQL  │ │ (PostgreSQL │ │ (PostgreSQL  │  │
│  │ schema)      │ │ schema)     │ │ schema)      │  │
│  └──────────────┘ └─────────────┘ └──────────────┘  │
└─────────────────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│              External APIs                          │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ Finnhub  │  │ OpenAI   │  │ IBKR TWS/Gateway  │  │
│  │ RSS      │  │ Groq     │  │                   │  │
│  │ Marketaux│  │          │  │                   │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### 3.1 Process Model (Mandatory Decoupling)

For runtime decoupling, these modules run as separate OS processes (or separate containers):

1. `news_fetcher` process: polls providers, normalizes and deduplicates, then publishes to `news_raw_queue`
2. `message_broker` process: standalone queue backend that mediates all inter-process communication
3. `analyzer_worker` process: consumes news from `news_raw_queue`, performs scoring/LLM enrichment, then publishes trade signals to `signal_queue`
4. `trade_executor` process: consumes signals from `signal_queue`, applies risk checks, and executes orders via IBKR

In-process queues are not used for production because they do not provide durability or cross-process isolation. The message broker must run independently of all producer and consumer processes.

### 3.2 Message Broker (Standalone Component)

The message broker is a dedicated process that owns all queue state. Service processes connect to it as clients; they do not share memory or in-process queues.

Supported backends:

- **Redis Streams** (default): simplest setup, supports consumer groups and replay; run as a separate Docker container with persistence enabled
- **RabbitMQ**: stronger routing and acknowledgment controls; higher operational overhead

The broker exposes three named queues:

| Queue                  | Producer         | Consumer(s)                                    |
|------------------------|------------------|------------------------------------------------|
| `news_raw_queue`       | NewsFetcher      | AnalyzerWorker, NarrativeAggregator (planned)  |
| `signal_queue`         | AnalyzerWorker   | TradeExecutor                                  |
| `failed_messages_dlq`  | any (on failure) | ops / alerting                                 |

`news_raw_queue` has multiple independent consumer groups: each consumer must receive every message and track its own offset. This rules out a simple competing-consumer (work queue) pattern for that stream and requires a broker with native consumer group support.

Default recommendation: Redis Streams in a dedicated Docker container, with `appendonly yes` persistence and an explicit dead-letter stream. Redis Streams consumer groups (`XREADGROUP`) provide native support for multiple independent consumers on the same stream, which is required by `news_raw_queue`.

### Layer Responsibilities

| Layer           | Directory          | Responsibility                                         |
|-----------------|--------------------|---------------------------------------------------------|
| UI              | `src/ui`           | Dashboard, alerts, manual override controls             |
| Services        | `src/services`     | Business logic: news fetching, LLM analysis, trading    |
| Repositories    | `src/repositories` | PostgreSQL persistence with schema-per-component ownership |
| Utilities       | `src/utils`        | Rate limiting, retry logic, logging helpers             |
| Config          | `src/config.py`    | Environment variables, API keys, thresholds             |

### 3.3 Tech Stack

The system uses the following baseline stack:

- **Language/runtime (services):** Python 3.13+
- **UI framework:** React + TypeScript
- **UI build tooling:** Vite
- **Service/API layer:** Python service processes (process-based architecture)
- **Message broker:** Redis Streams
- **Database:** PostgreSQL 16+
- **Trading integration:** Interactive Brokers TWS/Gateway via `ib_insync`
- **LLM providers:** OpenAI gpt-4o-mini (primary), Groq/Gemini (fallback)
- **Local infrastructure/runtime:** Docker for broker and database, local process execution for services

---

## 4. Core Components

### 4.1 NewsService (`src/services/news_service.py`)

Fetches and normalizes news from multiple providers into a unified format.

**Responsibilities:**
- Poll Finnhub company news API at configurable intervals (default: every 2 minutes)
- Parse RSS feeds from Yahoo Finance and Reuters (every 5 minutes)
- Optionally query Marketaux for entity-tagged news
- Deduplicate articles by title similarity (fuzzy matching)
- Filter articles by watchlist tickers and configurable keywords
- Store raw articles in PostgreSQL via `NewsRepo`

**Unified News Schema:**

```python
@dataclass
class NewsArticle:
    id: str                  # SHA-256 hash of source + url
    source: str              # "finnhub" | "marketaux" | "rss"
    headline: str
    summary: str
    url: str
    tickers: list[str]       # Associated stock tickers
    published_at: datetime
    fetched_at: datetime
    sentiment_source: float | None  # Provider-supplied sentiment if available
```

**Rate Limiting:**
- Finnhub free: 60 calls/min → fetch top news every 2 min, use 1 call each
- RSS: no limit → poll every 5 min
- Marketaux free: 100/day → use only for tickers not covered by Finnhub

### 4.2 AIService (`src/services/ai_service.py`)

Analyzes news articles and produces structured trading signals.

**Responsibilities:**
- Build prompts from news articles with relevant context (ticker, sector, recent price)
- Call LLM API to extract: sentiment score, relevance, urgency, suggested action
- Aggregate multiple articles about the same ticker into a single signal
- Cache LLM responses to avoid redundant calls for duplicate/similar news
- Fall back to Groq/Gemini if OpenAI quota is exceeded

**LLM Output Schema:**

```python
@dataclass
class NewsAnalysis:
    article_id: str
    ticker: str
    sentiment: float         # -1.0 (very bearish) to +1.0 (very bullish)
    relevance: float         # 0.0 to 1.0
    urgency: str             # "immediate" | "today" | "this_week" | "informational"
    suggested_action: str    # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    reasoning: str           # LLM's explanation (stored for audit)
    confidence: float        # 0.0 to 1.0
```

**Cost Control:**
- Skip articles with relevance < 0.3 (based on keyword pre-filter)
- Batch 3–5 articles about the same ticker into one LLM call
- Use gpt-4o-mini (not gpt-4o) for routine analysis
- Daily token budget: configurable, default 500K tokens/day (~$0.075/day)

### 4.3 TradeService (`src/services/trade_service.py`)

Validates trading decisions and executes them via IBKR.

**Responsibilities:**
- Apply risk management rules before execution (see Trading Strategies doc)
- Submit orders via IBKR TWS API / IB Gateway
- Monitor order status (filled, partial, rejected)
- Log all executions to `TradeRepo`
- Support manual override: pause bot, cancel pending orders

**IBKR Integration:**
- Use `ib_insync` Python library for IBKR TWS API
- Connect to IB Gateway (headless) or TWS (with UI)
- Paper trading mode for testing (IBKR provides free paper accounts)
- Market orders for urgent signals; limit orders for non-urgent

### 4.4 WorkflowOrchestrator (`src/services/workflow.py`)

Coordinates process startup, health checks, and queue-level flow control.

**Queue-Based Pipeline:**

```
1. NewsFetcher.publish(news_raw_queue)                    → queued raw news events
2. AnalyzerWorker.consume(news_raw_queue)                 → list[NewsAnalysis]
3. AnalyzerWorker.publish(signal_queue)                   → queued signal events
4. TradeExecutor.consume(signal_queue)                    → list[TradeDecision]
5. TradeExecutor.execute(decisions)                       → list[TradeExecution]
6. Repositories.persist(all)                              → audit trail
7. Failed messages after max retries                      → failed_messages_dlq
```

**Scheduling:**
- NewsFetcher polls every 2 minutes during market hours (9:30 AM – 4:00 PM ET)
- Reduce to every 15 minutes during pre/post-market
- No runs on weekends/holidays (use `exchange_calendars` library)

**Delivery semantics:**
- At-least-once delivery via explicit ACK after successful persistence
- Idempotent consumers via dedupe key (`article_id` for news, `decision_id` for signals)
- Retry policy: exponential backoff (3 attempts), then send to dead-letter queue

---

## 5. Data Model (PostgreSQL, Schema-Per-Component)

The system persists audit-grade records for the end-to-end pipeline in five PostgreSQL tables across component-owned schemas:

- `news_fetcher.t_news_articles`
- `analyzer_worker.t_news_analyses`
- `trade_executor.t_trade_decisions`
- `trade_executor.t_trade_executions`
- `shared.t_api_usage`

Table naming convention: all physical table names use prefix `t_`.

For full table definitions and constraints, see `docs/design/data-model.md`.

---

## 6. Configuration

All runtime settings are environment-driven (never hardcoded), grouped by:

- News provider keys and quotas
- IBKR connectivity
- Risk and trading constraints
- LLM budget and fallback behavior
- Queue backend and retry limits
- Operational flags (logging, paper trading)

For the complete configuration catalog and defaults, see `docs/design/configuration.md`.

---

## 7. Risk Management

Enforced in `RiskManager` (part of the service layer) before any trade execution:

| Rule                        | Default               | Purpose                                    |
|-----------------------------|-----------------------|--------------------------------------------|
| Max position size           | $1,000                | Limit exposure per ticker                  |
| Max daily trades            | 10                    | Prevent overtrading                        |
| Max portfolio exposure      | $5,000                | Total capital at risk                      |
| Daily loss limit            | $200                  | Circuit breaker — pause bot                |
| Min confidence threshold    | 0.6                   | Skip low-confidence signals                |
| Min signal agreement        | 2 articles            | Require multiple sources to confirm signal |
| Cooldown per ticker         | 30 minutes            | Prevent rapid re-entry                     |
| Paper trading mode          | Enabled by default    | Test before going live                     |

---

## 8. Error Handling

| Failure                    | Behavior                                              |
|----------------------------|-------------------------------------------------------|
| News API down              | Log warning, continue with other sources              |
| News API rate limited      | Back off exponentially, switch to fallback source      |
| LLM API timeout            | Retry once, then skip article                         |
| LLM API quota exceeded     | Switch to fallback provider, log alert                |
| IBKR disconnected          | Queue decisions, retry connection every 30s, alert UI |
| Order rejected by IBKR     | Log reason, do not retry automatically                |
| PostgreSQL write failure   | Retry once, halt pipeline if persistent               |
| Daily loss limit reached   | Pause all trading, alert via UI, require manual reset |

---

## 9. Monitoring & UI

The UI (`src/ui`) provides:

- **Dashboard:** current positions, P&L, recent trades, bot status
- **News feed:** latest fetched articles with sentiment scores
- **Decision log:** every AI analysis and trade decision with reasoning
- **API usage:** token consumption, request counts, estimated costs
- **Controls:** pause/resume bot, cancel pending orders, manual trade entry
- **Alerts:** daily loss limit hit, API errors, unusual activity

---

## 10. Deployment

**Local deployment (recommended for private trader):**
- Run on a local machine or Raspberry Pi
- IB Gateway runs as a background process
- Bot runs as a Python service (systemd or Task Scheduler)
- PostgreSQL database stored locally (Docker recommended), with one schema per component

**Requirements:**
- Python 3.13+
- PostgreSQL 16+ (or compatible managed PostgreSQL)
- Interactive Brokers account (live or paper)
- IB Gateway or TWS installed
- Internet connection during market hours

---

## 11. Development Phases

| Phase | Scope                                           | Milestone                         |
|-------|-------------------------------------------------|-----------------------------------|
| 1     | NewsService (Finnhub + RSS), PostgreSQL storage | Fetching and storing news         |
| 2     | AIService with gpt-4o-mini, decision schema     | Generating trading signals        |
| 3     | TradeService with IBKR paper trading            | Executing paper trades            |
| 4     | WorkflowOrchestrator, scheduling                | End-to-end pipeline running       |
| 5     | Risk management, circuit breakers               | Safe automated trading            |
| 6     | UI dashboard                                    | Monitoring and manual overrides   |
| 7     | Backtesting framework                           | Validate strategies historically  |
| 8     | Live trading (after paper validation)           | Real money deployment             |

---

## 12. References

- [Finnhub API docs](https://finnhub.io/docs/api)
- [Marketaux API docs](https://www.marketaux.com/documentation)
- [EODHD API docs](https://eodhd.com/financial-apis)
- [ib_insync documentation](https://ib-insync.readthedocs.io/)
- [IBKR API reference](https://interactivebrokers.github.io/tws-api/)
- [OpenAI API pricing](https://openai.com/api/pricing/)
- Trading strategies: see [trading-strategies.md](trading-strategies.md)
- Database deployment spec: `docs/design/deployment/postgres-container.md`
- Queue deployment spec: `docs/design/deployment/redis-queue-container.md`
- Data model details: `docs/design/data-model.md`
- Configuration index: `docs/design/configuration.md`
- Component configuration specs: `docs/design/product-components/news-fetcher/configuration.md`, `docs/design/product-components/analyzer-worker/configuration.md`, `docs/design/product-components/trade-executor/configuration.md`, `docs/design/shared/configuration.md`

## 13. Source Code Organization and Core vs Product Boundary

To ensure the separation between reusable core technology and product-specific logic is maintained in the source code, the following rules and structure must be followed:

### 13.1 Source Directory Structure

- All domain-agnostic, reusable logic must be placed in `src/core_components/` (or `src/core/`).
- All trading/product-specific logic, orchestration, and business rules must be placed in `src/product_components/` (or `src/product/`.

### 13.2 Boundary Enforcement Rules

- **Core components** must not import or depend on any product-specific (trading) modules, APIs, or data models.
- **Product components** may depend on core components, but not vice versa.
- New code and refactors must follow this split.

#### Examples

| Belongs in core_components           | Belongs in product_components           |
|--------------------------------------|-----------------------------------------|
| Event normalization pipeline         | NewsFetcher orchestration logic         |
| Generic deduplication algorithms     | Watchlist-driven relevance filter       |
| Canonical event schema (domain-free) | Trading signal generation               |
| Pluggable source adapter interfaces  | IBKR order execution logic              |

### 13.3 Code Review and Code Generation Checklist

- Does this module depend on trading-specific APIs or business rules? If yes, it belongs in product_components.
- Does this module implement a generic interface (e.g., event normalization, deduplication, persistence abstraction)? If yes, it belongs in core_components.
- Are new files and refactors placed in the correct directory according to these rules?

This boundary must be enforced during both manual development and automated code generation. Any code generation or review process should explicitly check for and maintain this separation.
