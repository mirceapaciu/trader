# News-Trader One-Pager

## North Star

Build a trading system that turns real-time financial news into validated, risk-bounded trade decisions fast enough to act and strict enough to refuse.

The product wins when it consistently finds actionable news, converts it into a single canonical thesis card, and executes only when that card is approved, fresh, and complete.

## What We Are Building

An AI-assisted trading platform for a small private trader that:

1. Ingests financial news continuously from multiple sources.
2. Normalizes and deduplicates the incoming information.
3. Converts evidence into thesis cards with a fixed schema.
4. Rejects anything that does not satisfy the contract.
5. Executes approved trades through Interactive Brokers.
6. Shows the full pipeline in a UI for review, monitoring, and manual override.

## Non-Negotiables

The product is defined by one rule: **No Thesis Card, No Trade**.

That means:

- Every trade candidate must map to exactly one thesis card.
- The thesis card must pass deterministic validation (schema, field presence, ranges, freshness) before execution can start. This validation is fully automated.
- Missing, stale, incomplete, or unapproved cards must fail closed.
- Execution must never bypass card validation.
- Approval (the approve/reject decision) can be done either automatically via explicit policy rules or manually in UI.
- The system must stay under a target monthly external API budget of **100 EUR or less**.

## Product Principles

- **Evidence first.** Decisions come from recent, traceable news evidence, not free-form speculation.
- **Deterministic before clever.** Validation rules and state transitions must be explicit and reproducible.
- **Fail closed.** If confidence, freshness, or risk constraints are unclear, do not trade.

## What Success Looks Like

Trades are profitable. Everything else is a means to that end. 
