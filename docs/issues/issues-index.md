# Issues Index

| id | title | status | detail_file |
| --- | --- | --- | --- |
| 260525-01 | Implement core event-ingestion-engine asset | resolved | docs/issues/issues-detail/260525-01.md |
| 260525-02 | Add PostgreSQL integration tests | resolved | docs/issues/issues-detail/260525-02.md |
| 260525-03 | Add Redis integration tests | resolved | docs/issues/issues-detail/260525-03.md |
| 260529-01 | Add NewsFetcher provider cycle status | resolved | docs/issues/issues-detail/260529-01.md |
| 260601-01 | Add simulation-first filter quality infrastructure | new | docs/issues/issues-detail/260601-01.md |
| 260615-01 | Remove ThesisBuilder direct database access to NewsFetcher | resolved | docs/issues/issues-detail/260615-01.md |
| 260615-02 | Remove ThesisBuilder direct database access to Shared contracts | resolved | docs/issues/issues-detail/260615-02.md |
| 260615-03 | Remove NewsFetcher direct database access to Shared contracts | resolved | docs/issues/issues-detail/260615-03.md |
| 260615-04 | Remove MarketData direct database access to Shared contracts | resolved | docs/issues/issues-detail/260615-04.md |
| 260615-05 | Remove Filter Quality Evaluator direct database access to NewsFetcher | new | docs/issues/issues-detail/260615-05.md |
| 260615-06 | Remove Filter Quality Evaluator direct database access to Shared contracts | new | docs/issues/issues-detail/260615-06.md |
| 260615-07 | Remove Monitoring UI direct database access to NewsFetcher | new | docs/issues/issues-detail/260615-07.md |
| 260615-08 | Remove Monitoring UI direct database access to ThesisBuilder | new | docs/issues/issues-detail/260615-08.md |
| 260615-09 | Remove Monitoring UI direct database access to Filter Quality Evaluator | new | docs/issues/issues-detail/260615-09.md |
| 260615-10 | Remove Monitoring UI direct database access to Shared contracts | new | docs/issues/issues-detail/260615-10.md |
| 260615-11 | Add Monitoring UI watchlist editor with ticker lookup and alias management | resolved | docs/issues/issues-detail/260615-11.md |
| 260626-01 | Repo-wide pytest collection failure due to duplicate test basenames and missing __init__.py | new | docs/issues/issues-detail/260626-01.md |
| 260627-01 | Move thesis reprocessing out of Monitoring UI into ThesisBuilder via Redis command stream | new | docs/issues/issues-detail/260627-01.md |
| 260628-01 | Regeneration backtest mode always fails and is an unimplemented no-op | new | docs/issues/issues-detail/260628-01.md |
| 260628-02 | Backtest background-thread failures leave the UI stuck on "Running…" forever | new | docs/issues/issues-detail/260628-02.md |
| 260707-01 | Backtester should invoke TradeExecutor's pure decision logic instead of reimplementing diverging trading rules | resolved | docs/issues/issues-detail/260707-01.md |
| 260707-02 | Backtester must persist per-trade excursion diagnostics required by backtest verification | resolved | docs/issues/issues-detail/260707-02.md |
| 260708-01 | Token-budget exhaustion in regeneration backtests must be visible, not silent | new | docs/issues/issues-detail/260708-01.md |
| 260708-02 | Cache LLM article analyses so repeated regeneration backtests don't re-pay for identical prompts | resolved | docs/issues/issues-detail/260708-02.md |
| 260708-03 | Historize live market-context snapshots so regeneration can replay the exact context live analyses saw | new | docs/issues/issues-detail/260708-03.md |
| 260708-04 | Monitoring UI PowerShell start script does not capture backend process output | resolved | docs/issues/issues-detail/260708-04.md |
| 260709-01 | High-price tickers are structurally unsizeable; generation produces cards the executor can never trade | new | docs/issues/issues-detail/260709-01.md |
| 260709-02 | Two-tier pre-filter (deterministic screen + small-LLM triage) before paying for full LLM analysis | new | docs/issues/issues-detail/260709-02.md |
| 260709-03 | Implement already-priced move suppression (close the documented §1.4 known gap) | new | docs/issues/issues-detail/260709-03.md |
| 260709-04 | Confidence gates never bind — measure LLM confidence calibration, then recalibrate or replace them | new | docs/issues/issues-detail/260709-04.md |
| 260709-05 | docs/trading-strategies.md describes a system that does not exist — align it with the implementation | new | docs/issues/issues-detail/260709-05.md |
| 260709-06 | Add a strong-model card-synthesis pass at evidence-window satisfaction (tier 2) | new | docs/issues/issues-detail/260709-06.md |
