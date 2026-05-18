# AnalyzerWorker Data Model

Tables owned by the AnalyzerWorker component.
PostgreSQL schema: `analyzer_worker`.

## Logical Model

### `t_news_analyses`

Purpose:
- Durable record of AnalyzerWorker outputs for each analyzed news item and ticker.

Logical fields:
- `id` (primary key): analysis record identity.
- `article_id`: source article identity from NewsFetcher.
- `ticker`: analyzed instrument symbol.
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
- `article_id` must reference an existing NewsFetcher article.
- Analysis records are append-oriented for auditability.
- Scores and classifications must be derived from deterministic analyzer policy for identical inputs when deterministic mode is enabled.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product-components/analyzer-worker/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
