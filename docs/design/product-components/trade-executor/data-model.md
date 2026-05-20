# TradeExecutor Data Model

Tables owned by the TradeExecutor component.
PostgreSQL schema: `trade_executor`.

## Logical Model

### `t_trade_decisions`

Purpose:
- Durable record of risk-evaluated trade decisions produced before broker execution.

Logical fields:
- `id` (primary key): decision record identity.
- `thesis_card_id`: required reference to analyzer thesis card identity.
- `ticker`: target instrument symbol.
- `exchange_code`: target exchange identifier used for market-data and broker routing.
- `action`: intended action (`buy`, `sell`, `hold`).
- `quantity`: intended order quantity.
- `order_type`: order type (`market`, `limit`, or policy-defined extension).
- `limit_price`: optional limit price when order type requires it.
- `signal_strength`: optional normalized signal strength.
- `source_analysis_ids`: references to analysis evidence used for the decision.
- `risk_check_passed`: final risk gate result.
- `risk_check_details`: optional risk rationale.
- `decided_at`: decision timestamp.

Behavioral constraints:
- Decision records are append-oriented for auditability.
- Decisions must capture risk gate outcome before execution attempt.
- Instrument identity is the pair (`ticker`, `exchange_code`) for execution routing.
- `thesis_card_id` is mandatory for all non-hold actions.
- Decision is executable only when shared review state for `thesis_card_id` is `approved` and not expired.

### `t_trade_executions`

Purpose:
- Durable record of broker execution lifecycle for each decision.

Logical fields:
- `id` (primary key): execution record identity.
- `decision_id`: owning trade decision identity.
- `ibkr_order_id`: optional broker-native order identifier.
- `status`: execution status (`submitted`, `filled`, `partial`, `rejected`, `cancelled`).
- `fill_price`: optional average fill price.
- `fill_quantity`: optional filled quantity.
- `commission`: optional total commission.
- `executed_at`: optional execution timestamp.
- `error_message`: optional execution/broker error detail.

Behavioral constraints:
- `decision_id` must reference an existing trade decision.
- Execution status transitions must be captured as durable updates or append events according to implementation policy.
- Every execution attempt must remain traceable to exactly one thesis card through its decision row.

## Notes

- Executable PostgreSQL DDL is maintained in `src/product-components/trade-executor/db/schema.sql`.
- Design docs define logical ownership and behavior constraints; runtime DDL must come from source-managed SQL/migrations.
