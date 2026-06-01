# Configuration Specification

This file is the configuration index.
Configuration is split by ownership boundary so each process can evolve independently.

## Configuration Map

- Shared settings: `docs/design/shared/configuration.md`
- NewsFetcher settings: `docs/design/product_components/news-fetcher/configuration.md`
- AnalyzerWorker settings: `docs/design/product_components/analyzer-worker/configuration.md`
- TradeExecutor settings: `docs/design/product_components/trade-executor/configuration.md`
- Monitoring UI settings: `docs/design/product_components/monitoring-ui/configuration.md`
- Filter Quality Evaluator settings: `docs/design/product_components/filter-quality-evaluator/configuration.md`

## Separation Rules

- Add a variable to the component file that owns its lifecycle.
- Keep cross-cutting settings only in `docs/design/shared/configuration.md`.
- Do not duplicate the same variable across files.
- The overview should reference this index, not individual variables.