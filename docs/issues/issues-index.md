# Issues Index

| id | title | status | detail_file |
| --- | --- | --- | --- |
| 260525-01 | Implement core event-ingestion-engine asset | resolved | docs/issues/issues-detail/260525-01.md |
| 260525-02 | Add PostgreSQL integration tests | resolved | docs/issues/issues-detail/260525-02.md |
| 260525-03 | Add Redis integration tests | resolved | docs/issues/issues-detail/260525-03.md |
| 260529-01 | Add NewsFetcher provider cycle status | resolved | docs/issues/issues-detail/260529-01.md |
| 260601-01 | Add simulation-first filter quality infrastructure | resolved | docs/issues/issues-detail/260601-01.md |
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
| 260626-01 | Repo-wide pytest collection failure due to duplicate test basenames and missing __init__.py | resolved | docs/issues/issues-detail/260626-01.md |
| 260627-01 | Move thesis reprocessing out of Monitoring UI into ThesisBuilder via Redis command stream | resolved | docs/issues/issues-detail/260627-01.md |
| 260628-01 | Regeneration backtest mode always fails and is an unimplemented no-op | resolved | docs/issues/issues-detail/260628-01.md |
| 260628-02 | Backtest background-thread failures leave the UI stuck on "Running…" forever | new | docs/issues/issues-detail/260628-02.md |
| 260707-01 | Backtester should invoke TradeExecutor's pure decision logic instead of reimplementing diverging trading rules | resolved | docs/issues/issues-detail/260707-01.md |
| 260707-02 | Backtester must persist per-trade excursion diagnostics required by backtest verification | resolved | docs/issues/issues-detail/260707-02.md |
| 260708-01 | Token-budget exhaustion in regeneration backtests must be visible, not silent | resolved | docs/issues/issues-detail/260708-01.md |
| 260708-02 | Cache LLM article analyses so repeated regeneration backtests don't re-pay for identical prompts | resolved | docs/issues/issues-detail/260708-02.md |
| 260708-03 | Historize live market-context snapshots so regeneration can replay the exact context live analyses saw | resolved | docs/issues/issues-detail/260708-03.md |
| 260708-04 | Monitoring UI PowerShell start script does not capture backend process output | resolved | docs/issues/issues-detail/260708-04.md |
| 260709-01 | High-price tickers are structurally unsizeable; generation produces cards the executor can never trade | resolved | docs/issues/issues-detail/260709-01.md |
| 260709-02 | Two-tier pre-filter (deterministic screen + small-LLM triage) before paying for full LLM analysis | resolved | docs/issues/issues-detail/260709-02.md |
| 260709-03 | Implement already-priced move suppression (close the documented §1.4 known gap) | resolved | docs/issues/issues-detail/260709-03.md |
| 260709-04 | Confidence gates never bind — measure LLM confidence calibration, then recalibrate or replace them | resolved | docs/issues/issues-detail/260709-04.md |
| 260709-05 | docs/trading-strategies.md describes a system that does not exist — align it with the implementation | resolved | docs/issues/issues-detail/260709-05.md |
| 260709-06 | Add a strong-model card-synthesis pass at evidence-window satisfaction (tier 2) | resolved | docs/issues/issues-detail/260709-06.md |
| 260709-07 | Soft dedupe is scoped per source; the same story ingested via two providers counts twice | new | docs/issues/issues-detail/260709-07.md |
| 260714-01 | Binary instrument_is_subject gate mishandles supply-chain read-through articles | resolved | docs/issues/issues-detail/260714-01.md |
| 260714-02 | Watchlist instrument aliases lack common press names, breaking text-based attribution | resolved | docs/issues/issues-detail/260714-02.md |
| 260714-03 | subject_relation=direct is self-reported by the LLM; misclassified sector stories bypass the indirect-evidence gates | resolved | docs/issues/issues-detail/260714-03.md |
| 260715-01 | Group evidence windows by story: narrative assignment, per-story cards, post-card corroboration | resolved | docs/issues/issues-detail/260715-01.md |
| 260715-02 | Analysis prompt presents provider feed tags as ground-truth attribution; teaser articles form fully off-instrument cards | resolved | docs/issues/issues-detail/260715-02.md |
| 260715-03 | Article publication time is treated as event time; recap articles of old events pass every freshness and already-priced gate | resolved | docs/issues/issues-detail/260715-03.md |
| 260715-04 | Anchor-evidence gate runs before story assignment and is key-scoped; a new_story indirect analysis seeds an anchor-less window | resolved | docs/issues/issues-detail/260715-04.md |
| 260715-05 | Story assignment is taken from the LLM on trust; a misassigned article joined an unrelated story's window and its evidence published on the card | resolved | docs/issues/issues-detail/260715-05.md |
| 260716-01 | Story-assignment verification is bag-of-words; same-company/same-domain articles about different events still pass on a single generic token | resolved | docs/issues/issues-detail/260716-01.md |
| 260722-01 | Zero-overlap story verification fragments paraphrased coverage of the same event | resolved | docs/issues/issues-detail/260722-01.md |
| 260723-01 | Implement versioned event_identity schema with controlled taxonomy and user-visible gaps | new | docs/issues/issues-detail/260723-01.md |
| 260728-01 | Add operator workflow for accepting controlled event-identity values | new | docs/issues/issues-detail/260728-01.md |
| 260729-01 | Load revisioned event taxonomy dynamically at runtime | resolved | docs/issues/issues-detail/260729-01.md |
| 260729-02 | Add asynchronous taxonomy-decision commands and trusted actor identity | resolved | docs/issues/issues-detail/260729-02.md |
| 260729-03 | Execute bounded and resumable event-taxonomy backfill jobs | resolved | docs/issues/issues-detail/260729-03.md |
| 260729-04 | Complete the Monitoring UI taxonomy decision workflow | resolved | docs/issues/issues-detail/260729-04.md |
