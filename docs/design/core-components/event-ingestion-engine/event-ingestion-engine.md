# Event Ingestion Engine Specification

## 1. Named Core Asset

**Real-time Event Ingestion and Signal Preprocessing Engine**

This core asset is a reusable, domain-agnostic engine that ingests external events, normalizes them into canonical records, performs relevance filtering and deduplication, requires persistence of an audit trail through a storage adapter, and publishes idempotent events for downstream processing.

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

### 3.1.1 Checkpoint Persistence Contract (v1)

Checkpoints define replay-safe progress for each inbound source adapter.

Checkpoint storage location:
- Checkpoints must be stored in a durable owner table in the component schema using the engine (for example `news_fetcher.t_source_checkpoints`).
- In-memory-only checkpoints are not valid for production.

Logical checkpoint record fields:
- `source_key` (TEXT): stable adapter identifier (for example `finnhub`, `marketaux`, `rss:reuters`).
- `cursor_value` (JSON): provider-specific incremental cursor (timestamp, id, page token, offset, or composite).
- `cursor_updated_at` (TIMESTAMPTZ): UTC time when cursor was last advanced.
- `version` (BIGINT): optimistic concurrency version.
- `updated_at` (TIMESTAMPTZ): UTC row update timestamp.

Uniqueness and ownership rules:
- Exactly one active checkpoint row must exist per `source_key`.
- `source_key` must be unique in the checkpoint table.
- Only the owning ingestion process may mutate its checkpoint rows.

Update rules:
- Checkpoint advancement must be atomic with optimistic concurrency (`version` compare-and-set).
- A checkpoint may advance only after the batch is durably persisted and publication obligations are completed (published successfully or explicitly dead-lettered).
- Failed batches must not advance the checkpoint.
- Retried batches must reuse the previous checkpoint input.

Replay semantics:
- Re-processing from an older checkpoint is supported and expected during recovery/backfill.
- Replays must be safe because persistence and publication are idempotent.
- Manual checkpoint rewind is allowed as an operational action and must be auditable.

Recovery semantics:
- On restart, adapters must resume from stored checkpoint; if none exists, use configured bootstrap policy (for example `latest`, `lookback_window`, or `from_start`).
- If checkpoint data is malformed, processing must stop for that source and emit a non-transient configuration/data error.

### 3.2 Canonicalization Interface

Responsibility:
- Convert source events into one canonical event schema.

Contract:
- Input: source event.
- Output: canonical event with deterministic id.
- Guarantees: normalized fields, UTC timestamps, deterministic identity.

### 3.2.1 Canonical Core Event Schema (v1)

The canonical core event schema is domain-neutral and must be used by all source adapters before filtering, deduplication, persistence, and publication.

Required fields:
- `id` (TEXT): deterministic identifier for the canonical event.
- `source` (TEXT): logical source name (for example provider or feed name).
- `source_event_id` (TEXT): original source identifier when available; otherwise a deterministic fallback derived from source payload.
- `canonical_locator` (TEXT): normalized source locator (for example canonical URL or stable resource path).
- `title` (TEXT): normalized primary headline or event title.
- `occurred_at` (TIMESTAMPTZ): source event time normalized to UTC.
- `ingested_at` (TIMESTAMPTZ): ingest processing time normalized to UTC.
- `payload_version` (TEXT): canonical schema version, default `1.0`.

Optional fields:
- `summary` (TEXT): short normalized summary text.
- `content_text` (TEXT): full content text.
- `entities` (JSON): normalized list of extracted entities or tags.
- `attributes` (JSON): domain-neutral key-value attributes.
- `extensions` (JSON): product-specific optional extensions that must not change core-field semantics.

Deterministic identity rules:
- `id` must be stable for equivalent input events across retries.
- `id` generation must use normalized inputs only.
- Any non-semantic source variations (for example tracking query parameters or whitespace differences) must not change `id`.

Normalization rules:
- All timestamps must be UTC.
- Text fields must be trimmed.
- Empty strings in optional fields must be converted to null.
- `canonical_locator` must be normalized before dedupe and id generation.

Portability rules:
- Core processing logic may depend only on required and optional core fields above.
- Product-specific processing must read custom data from `extensions` only.
- New schema versions must preserve backward compatibility for required fields or publish with an explicit major version increment.

### 3.3 Filtering and Deduplication Interface

Responsibility:
- Apply relevance policy and duplicate suppression.

Contract:
- Input: canonical event plus policy configuration and recent lookback data.
- Output: accepted or rejected decision with reason code.
- Guarantees: deterministic strong dedupe and configurable soft dedupe.

### 3.3.1 Soft Deduplication Contract (v1)

This contract defines the normative default implementation for soft duplicate suppression. Implementations may add alternative algorithms later, but `weighted_text_locator_v1` is the required baseline and must be supported by every engine build.

Required default algorithm:
- `algorithm`: `weighted_text_locator_v1`
- Purpose: reject near-duplicate canonical events that are not caught by strong dedupe.
- Evaluation order:
	1. run strong dedupe first,
	2. build a bounded lookback candidate set,
	3. gate candidates using deterministic hard filters,
	4. compute weighted similarity score,
	5. reject when best score is greater than or equal to threshold.

Required normalization pipeline:
- Soft dedupe must compare canonicalized core fields only.
- For `title` and `summary`:
	- normalize Unicode to NFKC,
	- lowercase,
	- trim leading and trailing whitespace,
	- collapse internal whitespace to a single ASCII space,
	- replace punctuation runs with a single space,
	- tokenize on spaces,
	- drop empty tokens.
- For `canonical_locator`:
	- lowercase scheme and host,
	- remove URL fragment,
	- remove known tracking query parameters (`utm_*`, `gclid`, `fbclid`),
	- remove default ports,
	- normalize repeated slashes in path,
	- remove trailing slash except for root path.
- Missing optional text values must be treated as null and scored as empty token sets.
- `ingested_at`, retry metadata, transport headers, and any product-specific extension fields must not participate in scoring.

Required lookback candidate set:
- Lookback candidates must be taken from persisted canonical events only.
- Default `lookback_window`: 72 hours measured backward from the candidate event `occurred_at`.
- Candidate set must be restricted to rows where `source` matches exactly.
- Candidate set must be ordered deterministically before scoring by `occurred_at` ascending, then `id` ascending.

Required hard filters before scoring:
- Exact `canonical_locator` match alone must not be treated as strong dedupe unless it also satisfies the strong dedupe identity contract for the canonical event.
- Reject candidate from comparison if absolute `occurred_at` difference exceeds the configured `max_time_delta_hours` (default `72`).
- Reject candidate from comparison if the title token overlap count is `0`.
- Only candidates surviving all hard filters may enter similarity scoring.

Required score function:
- Title similarity `title_score`: Jaccard similarity of normalized `title` token sets.
- Summary similarity `summary_score`: Jaccard similarity of normalized `summary` token sets. If either summary is null, use `0.0`.
- Locator similarity `locator_score`: `1.0` when normalized `canonical_locator` matches exactly, otherwise `0.0`.
- Final score:

$$
score = 0.70 \cdot title\_score + 0.20 \cdot summary\_score + 0.10 \cdot locator\_score
$$

- Default rejection threshold: `0.85`.
- Score must be rounded only for reporting. Threshold comparison must use full internal precision.

Required winner selection and tie-breaks:
- Compute score for every surviving candidate and choose the highest score.
- If multiple candidates share the same highest score, choose the candidate with:
	1. exact locator match first,
	2. higher `title_score`,
	3. earlier `occurred_at`,
	4. lexical lowest `id`.
- Tie-break sequence must be applied exactly in the order above.

Decision contract:
- Input:
	- canonical event,
	- soft dedupe policy (`enabled`, `algorithm`, `threshold`, `lookback_window`, `max_time_delta_hours`),
	- lookback candidate set derived from persisted canonical events.
- Output:
	- `accepted` boolean,
	- `reason_code` in `{accepted_unique, rejected_strong_duplicate, rejected_soft_duplicate}`,
	- optional `matched_event_id`,
	- optional `similarity_score`,
	- optional `algorithm_version`.
- If no candidate survives hard filters, the event must be accepted as `accepted_unique`.
- If best score is greater than or equal to threshold, the event must be rejected as `rejected_soft_duplicate`.
- If best score is below threshold, the event must be accepted as `accepted_unique`.

Determinism requirements:
- For identical normalized inputs, policy configuration, and persisted lookback data, the soft dedupe decision must be identical across retries and replays.
- Randomness, wall-clock time, non-deterministic iteration order, and storage retrieval order must not affect outcomes.
- Reprocessing the same event under unchanged policy must reproduce the same matched candidate, score, and reason code.

Replay and auditability requirements:
- The engine must emit structured audit data for every scored decision including `algorithm`, `algorithm_version`, `threshold`, `lookback_window`, `max_time_delta_hours`, `matched_event_id`, `title_score`, `summary_score`, `locator_score`, and final `score`.
- Policy changes are allowed to change future outcomes but must be versioned and auditable.

### 3.4 Persistence Interface

Responsibility:
- Define the contract for durably storing accepted canonical events via an owning component's storage adapter.

Contract:
- Input: accepted canonical event and storage metadata.
- Output: persisted record identity, status, and storage checkpoint outcome.
- Guarantees:
	- idempotent upsert semantics,
	- preserved first-seen timestamp,
	- no duplicate side effects on replay,
	- durable acknowledgement only after the owning adapter confirms the write.

Ownership rules:
- The core engine defines what must be persisted and when persistence is considered complete.
- The product component owns the concrete repository/table implementation and database schema.
- The core engine must not depend on any specific product component table name or SQL DDL.

Failure semantics:
- If persistence fails transiently, the owning adapter may retry according to configured policy.
- If persistence cannot be completed, the batch must not be marked successful and the checkpoint must not advance.
- Persistence failures must be surfaced as non-success outcomes to the orchestration layer.

### 3.5 Event Publication Interface

Responsibility:
- Publish persisted events to downstream consumers.

Contract:
- Input: persisted canonical event.
- Output: event envelope published to broker.
- Guarantees: at-least-once delivery with dedupe key for consumer idempotency.

### 3.5.1 Event Envelope Contract (v1)

All published events must use this envelope shape.

Required metadata fields:
- `event_id` (TEXT): unique envelope id for this published event. (message instance identity)
- `event_type` (TEXT): semantic type name, for example `news.article.created`.
- `event_version` (TEXT): envelope schema version, default `1.0`.
- `occurred_at` (TIMESTAMPTZ): time when the domain event occurred, UTC.
- `published_at` (TIMESTAMPTZ): time when the envelope was published, UTC.
- `producer` (TEXT): logical producer name, for example `news_fetcher`.
- `dedupe_key` (TEXT): deterministic idempotency key for consumer dedupe. (semantic idempotency identity)
- `payload` (JSON): event payload object.

Optional metadata fields:
- `correlation_id` (TEXT): trace id for multi-step workflows.
- `causation_id` (TEXT): parent event id that caused this event.
- `partition_key` (TEXT): stable key for ordered routing where supported.
- `retry_count` (INTEGER): number of publish retries already attempted.
- `headers` (JSON): transport-independent extension metadata.

Field constraints:
- `event_id` must be globally unique per published envelope.
- `event_type` must be stable and lowercase dot-separated (`domain.entity.action`).
- `event_version` must follow semantic version format `MAJOR.MINOR`.
- `occurred_at` and `published_at` must be UTC RFC 3339 timestamps.
- `published_at` must be greater than or equal to `occurred_at`.
- `dedupe_key` must be deterministic for semantically identical events.
- `payload` must be valid JSON object (not array or scalar).

Serialization and transport rules:
- Envelope body must be UTF-8 JSON.
- Metadata must be carried in the envelope body, not broker-specific headers only.
- Transport-specific headers are allowed but must not redefine core metadata semantics.
- Unknown metadata fields must be ignored by consumers unless explicitly configured as required.

Versioning policy:
- Minor version (`1.x`) may add optional fields only.
- Major version (`2.0+`) may change required fields or semantics.
- Producers must not remove or repurpose required fields within the same major version.
- Consumers must reject envelopes with unsupported major versions.

Compatibility rules:
- Backward compatibility target: a newer producer in the same major version must remain consumable by older consumers.
- Forward compatibility target: consumers must ignore unknown optional fields in the same major version.
- Required-field validation failure must route the envelope to dead-letter handling with structured reason codes.

Validation and failure handling:
- Envelopes missing any required field are invalid.
- Envelopes with invalid timestamp format or non-JSON payload are invalid.
- Invalid envelopes must not be retried indefinitely; they must be classified as non-transient and dead-lettered.
- Validation failures must include `event_id` when available, `event_type` when available, and a machine-readable `error_code`.

Idempotency rules:
- Producer must set `dedupe_key` using deterministic canonical-event identity.
- Consumer idempotency must be based on `dedupe_key`, not transient broker delivery metadata.
- Replays must preserve `dedupe_key` and `event_type` semantics.

Reference envelope example:

```json
{
	"event_id": "evt_01J7V7V8V9W0X1Y2Z3A4B5C6D",
	"event_type": "news.article.created",
	"event_version": "1.0",
	"occurred_at": "2026-05-17T09:30:10Z",
	"published_at": "2026-05-17T09:30:12Z",
	"producer": "news_fetcher",
	"dedupe_key": "art_4f8c8b9d0c6e...",
	"correlation_id": "run_20260517_0930",
	"payload": {
		"id": "art_4f8c8b9d0c6e...",
		"source": "finnhub",
		"title": "Company X beats expectations",
		"summary": "Revenue and guidance were above consensus.",
		"occurred_at": "2026-05-17T09:29:50Z",
		"ingested_at": "2026-05-17T09:30:10Z"
	}
}
```

### 3.5.2 Transactional Boundary Contract: Persist -> Publish -> Checkpoint (v1)

This section is normative and defines the required consistency boundary between storage, broker publication, and source checkpoint advancement.

Required implementation pattern:
- Engine implementations must use a durable publication obligation record (transactional outbox pattern or an equivalent mechanism with the same guarantees).
- Persistence of accepted canonical events and creation of publication obligations must occur in one local storage transaction.
- Broker publication and checkpoint advancement must occur outside that local transaction.
- Two-phase commit across database and broker is not required and must not be assumed.

Logical publication obligation fields (minimum):
- `obligation_id` (TEXT): stable unique id for one publish obligation.
- `canonical_event_id` (TEXT): canonical event identity being published.
- `event_type` (TEXT): semantic event type.
- `dedupe_key` (TEXT): deterministic idempotency key for downstream consumers.
- `envelope_json` (JSON): full envelope body to publish.
- `status` (TEXT): `{pending, publishing, published, dead_lettered}`.
- `attempt_count` (INTEGER): publish attempt counter.
- `last_error_code` (TEXT nullable): last machine-readable publish error.
- `updated_at` (TIMESTAMPTZ): status update time.

Required processing sequence per batch:
1. Read input events from one source cursor.
2. Canonicalize, filter, dedupe, and build accepted canonical events.
3. In one storage transaction:
	- idempotently upsert accepted canonical events,
	- idempotently upsert corresponding publication obligations with `status = pending`,
	- commit.
4. Publish each pending obligation to the broker with retry policy.
5. Mark each obligation terminally as `published` or `dead_lettered`.
6. Advance source checkpoint only if every obligation created by this batch is in terminal success state (`published` or explicitly `dead_lettered` according to policy).

Batch-success and checkpoint gate:
- A batch is successful only when all accepted events are durably persisted and all their publish obligations are terminally resolved.
- Checkpoint compare-and-set (`version`) must execute only after batch success.
- Any non-terminal obligation (`pending` or `publishing`) blocks checkpoint advancement.

Failure matrix:
- Persist fails before transaction commit: no new obligations exist; do not publish; do not advance checkpoint.
- Persist commit succeeds but publish not attempted (crash/restart): obligations remain `pending`; on recovery resume publication; do not advance checkpoint before terminal resolution.
- Publish transient failure: obligation remains non-terminal until retry succeeds or retry policy exhausts; checkpoint blocked.
- Publish non-transient envelope failure: obligation must be marked `dead_lettered` with reason code; checkpoint may proceed only if policy allows dead-letter as completion.
- Checkpoint compare-and-set failure: treat as concurrency conflict, reload checkpoint, re-evaluate batch completion, retry compare-and-set without re-persisting duplicate rows.

Recovery requirements:
- On restart, engine must scan unresolved obligations (`pending`, `publishing`) and continue publication before processing newer cursor ranges for the same source.
- Re-publication of the same obligation must preserve envelope semantics (`event_type`, `dedupe_key`, payload identity).
- Recovery must be replay-safe through idempotent persistence and consumer dedupe.

Observability requirements:
- Emit counters for obligations created, published, dead-lettered, retried, and checkpoint-blocked.
- Emit structured logs containing `source_key`, batch identifier, checkpoint input cursor, terminal obligation counts, and checkpoint CAS result.

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
