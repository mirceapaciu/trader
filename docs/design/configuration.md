# Configuration Specification

This file is the configuration index.
Configuration is split by ownership boundary so each process can evolve independently.

## Configuration Map

- Shared settings: `docs/design/shared/configuration.md`
- NewsFetcher settings: `docs/design/news-fetcher/configuration.md`
- AnalyzerWorker settings: `docs/design/analyzer-worker/configuration.md`
- TradeExecutor settings: `docs/design/trade-executor/configuration.md`

## Separation Rules

- Add a variable to the component file that owns its lifecycle.
- Keep cross-cutting settings only in `docs/design/shared/configuration.md`.
- Do not duplicate the same variable across files.
- The overview should reference this index, not individual variables.