# AnalyzerWorker Data Model

Tables owned by the AnalyzerWorker component.
PostgreSQL schema: `analyzer_worker`.

## Logical Model

### `t_thesis_cards`

Purpose:
- Canonical trade-justification object produced from news analyses.

Logical fields:
- `id` (primary key): thesis card identity.
- `ticker`: target instrument symbol.
- `exchange_code`: target exchange identifier (prefer MIC, for example `XNAS`, `XNYS`).
- `direction`: proposed direction (`buy`, `sell`, `hold`).
- `time_horizon`: strategy horizon descriptor.
- `evidence`: exactly three evidence entries with article references.
- `confidence`: normalized confidence in [0, 1].
- `risk_max_loss_usd`: max tolerated loss for this thesis.
- `risk_stop_condition`: stop condition expression.
- `risk_invalidation_condition`: thesis invalidation expression.
- `expires_at`: card expiry timestamp.
- `created_at`: card creation timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- Must include exactly three evidence items.
- All evidence items must reference valid `news_fetcher.t_news_articles` rows.
- Cards that fail validation are not persisted as executable inputs.

### `t_news_analyses`

Purpose:
- Durable record of AnalyzerWorker outputs for each analyzed news item and ticker.

Logical fields:
- `id` (primary key): analysis record identity.
- `article_id`: source article identity from NewsFetcher.
- `ticker`: analyzed instrument symbol.
- `exchange_code`: analyzed exchange identifier (prefer MIC, for example `XNAS`, `XNYS`).
- `sentiment`: sentiment score for the ticker in article context.
- `relevance`: relevance score for the ticker.
- `urgency`: urgency classification.
- `suggested_action`: suggested trading action.
- `reasoning`: optional explanatory reasoning text.
- `confidence`: confidence score.
- `llm_model`: model identifier used for the analysis.
- `tokens_used`: optional token usage for the analysis call.
- `analyzed_at`: analysis timestamp.

Behavioral constraints:
- Instrument identity is the pair (`ticker`, `exchange_code`) for all downstream joins and lookups.
- `article_id` must reference an existing NewsFetcher article.
- Analysis records are append-oriented for auditability.
- Scores and classifications must be derived from deterministic analyzer policy for identical inputs when deterministic mode is enabled.
- Each executable thesis card must be traceable to one or more analysis records.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product-components/analyzer-worker/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
