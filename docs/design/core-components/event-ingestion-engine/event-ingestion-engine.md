# Event Ingestion Engine Specification

## 1. Named Core Asset

**Real-time Event Ingestion and Signal Preprocessing Engine**

This core asset is a reusable, domain-agnostic engine that ingests external events, normalizes them into canonical records, performs relevance filtering and deduplication, persists an audit trail, and publishes idempotent events for downstream processing.

## 2. Generic Problem Statement

Many systems need to process high-volume, heterogeneous external events in near real time while maintaining reliability, traceability, and low operational cost.

Common challenges this asset solves:
- Multiple external sources with inconsistent payload formats.
- Duplicate and near-duplicate events that create downstream noise.
- Need for deterministic, idempotent processing across retries.
- Need for clear separation between ingestion and interpretation/decision layers.
- Need for durable auditability before downstream actions are taken.

## 3. Public Interfaces

The asset exposes these stable interfaces.

### 3.1 Inbound Source Adapter Interface

Responsibility:
- Pull or receive events from one external source.
- Map source-specific payloads to a canonical event draft.

Contract:
- Input: source configuration and checkpoint.
- Output: list of source events with source metadata and timestamps.
- Error model: transient vs non-transient classification.

### 3.2 Canonicalization Interface

Responsibility:
- Convert source events into one canonical event schema.

Contract:
- Input: source event.
- Output: canonical event with deterministic id.
- Guarantees: normalized fields, UTC timestamps, deterministic identity.

### 3.3 Filtering and Deduplication Interface

Responsibility:
- Apply relevance policy and duplicate suppression.

Contract:
- Input: canonical event plus policy configuration and recent lookback data.
- Output: accepted or rejected decision with reason code.
- Guarantees: deterministic strong dedupe and configurable soft dedupe.

### 3.4 Persistence Interface

Responsibility:
- Store accepted canonical events durably.

Contract:
- Input: canonical event.
- Output: persisted record identity and status.
- Guarantees: idempotent upsert semantics and preserved first-seen timestamp.

### 3.5 Event Publication Interface

Responsibility:
- Publish persisted events to downstream consumers.

Contract:
- Input: persisted canonical event.
- Output: event envelope published to broker.
- Guarantees: at-least-once delivery with dedupe key for consumer idempotency.

### 3.6 Usage Accounting Interface

Responsibility:
- Record source usage and call metadata for quotas and cost control.

Contract:
- Input: provider call metadata.
- Output: usage record in shared usage table.

## 4. Portability Constraints

The core asset must stay portable across products and domains.

Design constraints:
- Domain-neutral canonical schema boundaries. Domain-specific fields must be optional extensions, not core requirements.
- Pluggable source adapters. Adding a source must not require core pipeline rewrites.
- Pluggable queue backend. Broker implementation is a deploy-time concern.
- Pluggable persistence backend abstraction, with PostgreSQL as current reference implementation.
- Stable event envelope contract with versioning.
- No dependency on trading-only APIs in the core modules.
- Config-driven policy controls (filters, dedupe thresholds, retry policy) rather than hardcoded rules.

Operational constraints:
- Must run as independent process(es) with clear health/readiness signals.
- Must support idempotent replay and recovery after restarts.
- Must provide auditable logs and metrics for every pipeline stage.

## 5. Non-Trading Use Cases

This core technology is reusable beyond financial trading.

Examples:
- Cybersecurity alert ingestion and deduplication from multiple threat feeds.
- E-commerce product and pricing feed normalization from multiple vendors.
- Job posting aggregation from multiple job boards.
- Real-estate listing consolidation from multiple listing providers.
- Industrial IoT event ingestion and preprocessing before anomaly detection.
- News and media monitoring for brand/reputation analysis.

## 6. Product-to-Core Boundary

In this repository:
- Core asset responsibility: ingest, normalize, filter, dedupe, persist, and publish events.
- Product-specific responsibility: trading analysis, risk policy, and order execution.

This boundary ensures the core asset can be extracted as reusable IP even if product direction changes.
