# Data Model Specification

This file is the data model index.
Schema definitions are split by component ownership so tables evolve with the components that own their lifecycle.
The persistence engine is PostgreSQL, and each component owns a dedicated PostgreSQL schema.

## Data Model Map

- Shared schema (`shared`): `docs/design/shared/data-model.md`
- NewsFetcher schema (`news_fetcher`): `docs/design/news-fetcher/data-model.md`
- AnalyzerWorker schema (`analyzer_worker`): `docs/design/analyzer-worker/data-model.md`
- TradeExecutor schema (`trade_executor`): `docs/design/trade-executor/data-model.md`

## Separation Rules

- Define each table in one owner file only.
- Each component owns exactly one PostgreSQL schema and creates tables only in that schema.
- Keep cross-cutting tables in schema `shared` via `docs/design/shared/data-model.md`.
- Foreign-key references are allowed across component files but table definitions are not duplicated.
- Overview should reference this index, not inline SQL definitions.