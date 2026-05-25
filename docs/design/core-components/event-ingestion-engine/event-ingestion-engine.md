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

### 3.2.2 Deterministic Id Generation Contract (v1)

This contract defines the required algorithm for canonical event `id` generation.

Required algorithm:
- `algorithm`: `sha256_canonical_identity_v1`
- Build `id_input` by joining the normalized values below using ASCII `|` in this exact order:
	1. `source`
	2. `source_event_id`
	3. `canonical_locator`
	4. `occurred_at` formatted as UTC RFC 3339 seconds (`YYYY-MM-DDTHH:MM:SSZ`)
	5. `payload_version`
- Compute `hex = SHA-256(UTF-8(id_input))` as lowercase hexadecimal.
- Set canonical `id = "cev_" + hex`.

Normalization and stability rules:
- Inputs must be normalized per section `3.2.1` before hash computation.
- The delimiter `|` inside field values must be escaped as `\|` before joining.
- Null values are encoded as an empty string.
- `ingested_at`, retry metadata, transport headers, and `extensions` must not affect `id`.
- For equivalent normalized inputs, generated `id` must be identical across retries, replays, and nodes.

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
- `claimed_by` (TEXT nullable): current publishing worker identity when claimed.
- `claim_expires_at` (TIMESTAMPTZ nullable): UTC lease-expiration timestamp for current claim.
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
- Publish non-transient envelope failure: obligation must be marked `dead_lettered` with reason code; checkpoint may proceed only if `dead_letter_completes_obligation=true`.
- Checkpoint compare-and-set failure: treat as concurrency conflict, reload checkpoint, re-evaluate batch completion, retry compare-and-set without re-persisting duplicate rows.

Recovery requirements:
- On restart, engine must scan unresolved obligations (`pending`, `publishing`) and continue publication before processing newer cursor ranges for the same source.
- Re-publication of the same obligation must preserve envelope semantics (`event_type`, `dedupe_key`, payload identity).
- Recovery must be replay-safe through idempotent persistence and consumer dedupe.

Observability requirements:
- Emit counters for obligations created, published, dead-lettered, retried, and checkpoint-blocked.
- Emit structured logs containing `source_key`, batch identifier, checkpoint input cursor, terminal obligation counts, and checkpoint CAS result.

### 3.5.2.1 Outbox Worker Concurrency and Claim Semantics (v1)

This section is normative and defines how one or more workers may safely publish obligations concurrently.

Worker identity and lease model:
- Every publisher process must have a stable runtime `worker_id` for its lifetime.
- Publishing ownership is lease-based and stored on each obligation (`claimed_by`, `claim_expires_at`).
- A claim grants exclusive publish rights only until `claim_expires_at`.

Required claim eligibility:
- A worker may claim an obligation only when either:
	1. `status = pending`, or
	2. `status = publishing` and `claim_expires_at < now_utc` (stale lease takeover).
- Terminal rows (`published`, `dead_lettered`) are never claimable.

Required atomic claim operation:
- Claiming must be one atomic compare-and-set database operation.
- Claim operation must set:
	- `status = publishing`,
	- `claimed_by = worker_id`,
	- `claim_expires_at = now_utc + outbox.claim.lease_ms`,
	- `updated_at = now_utc`.
- Claim selection order must be deterministic: `updated_at` ascending, then `obligation_id` ascending.
- Implementations may use SQL `FOR UPDATE SKIP LOCKED` or an equivalent mechanism, but equivalent single-row ownership guarantees are required.

Publish-attempt ownership rules:
- Only the worker currently owning the claim (`claimed_by = worker_id` and `claim_expires_at >= now_utc`) may attempt broker publish.
- `attempt_count` must increment exactly once per broker publish attempt.
- Long-running publish attempts must renew lease before expiry using atomic heartbeat update of `claim_expires_at`.

Terminal transition rules:
- Transition to `published` or `dead_lettered` must be conditional on active ownership (`claimed_by = worker_id`).
- Terminal transition must clear lease fields (`claimed_by = null`, `claim_expires_at = null`).
- If terminal update affects `0` rows, worker must treat the claim as lost and must not emit a second terminal update.

Lease expiry and takeover semantics:
- If a worker crashes or stalls past lease expiry, another worker may take over using stale-lease claim eligibility.
- Re-publication after takeover is allowed and expected; downstream idempotency is preserved by `dedupe_key`.
- A worker that detects claim loss must stop work on that obligation and continue with new claims.

Checkpoint and recovery interaction:
- Obligations in `publishing` with unexpired lease are non-terminal and block checkpoint advancement.
- Obligations in `publishing` with expired lease must be reclaimable during recovery scans.
- On restart, workers must prioritize reclaimable unresolved obligations before claiming newer obligations for the same source.

Single-instance compatibility:
- Single-worker deployments must follow the same claim and lease rules; behavior must remain correct if additional workers are later introduced.

### 3.5.3 Failure Taxonomy (v1)

Every engine implementation must classify failures using the codes below and emit the code in logs/metrics (`error_code`) and publication obligation state (`last_error_code`) when applicable.

| Error Code | Stage | Class | Detection Signal | Retryable | Terminal Action | Checkpoint Impact | Owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `SRC_TIMEOUT` | Source fetch | transient | source request timeout or socket timeout | yes | retry by `source_fetch_transient` policy | checkpoint unchanged | source adapter |
| `SRC_RATE_LIMIT` | Source fetch | transient | HTTP `429` or explicit provider quota signal | yes | retry by `source_fetch_transient` policy | checkpoint unchanged | source adapter |
| `SRC_UNAVAILABLE` | Source fetch | transient | HTTP `500/502/503/504` or connection reset | yes | retry by `source_fetch_transient` policy | checkpoint unchanged | source adapter |
| `SRC_AUTH_INVALID` | Source fetch | non_transient | HTTP `401/403` or invalid credentials response | no | fail source cycle, raise configuration/auth alert | checkpoint unchanged | source adapter + ops |
| `SRC_REQUEST_INVALID` | Source fetch | non_transient | HTTP `400/404` from stable endpoint usage | no | fail source cycle, require configuration fix | checkpoint unchanged | source adapter |
| `PAYLOAD_SCHEMA_INVALID` | Canonicalization | non_transient | required source payload fields missing or malformed | no | reject item and continue batch; emit structured validation log | checkpoint may still advance if all publish obligations for accepted items are terminally resolved | canonicalization layer |
| `CANONICAL_CONTRACT_INVALID` | Canonicalization | non_transient | canonical required field missing or invalid after normalization | no | reject item and continue batch; emit structured validation log | checkpoint may still advance if all publish obligations for accepted items are terminally resolved | canonicalization layer |
| `PERSIST_TRANSIENT` | Persistence | transient | transient database connection/session/timeout error | yes | retry by `persist_transient` policy | checkpoint blocked until persistence succeeds | storage adapter |
| `PERSIST_CONSTRAINT` | Persistence | non_transient | non-retriable integrity or schema contract violation | no | fail batch and mark non-success outcome | checkpoint blocked | storage adapter + schema owner |
| `PUBLISH_TRANSIENT` | Publication | transient | broker unavailable, publish timeout, temporary transport error | yes | retry by `publish_transient` policy | checkpoint blocked until obligation terminally resolved | publisher |
| `ENVELOPE_INVALID` | Publication | non_transient | required envelope field missing or invalid format | no | mark obligation `dead_lettered` with reason code | treated as terminal only when `dead_letter_completes_obligation=true` | publisher |
| `PUBLISH_RETRY_EXHAUSTED` | Publication | terminal_exhausted | transient publish retries exhausted | no | mark obligation `dead_lettered` with exhaustion metadata | treated as terminal only when `dead_letter_completes_obligation=true` | publisher |
| `CHECKPOINT_CAS_CONFLICT` | Checkpoint | transient | optimistic concurrency compare-and-set mismatch | yes | retry by `checkpoint_cas_conflict` policy without re-persisting | checkpoint not advanced until CAS succeeds | checkpoint manager |
| `CHECKPOINT_DATA_INVALID` | Checkpoint | non_transient | malformed or unsupported checkpoint cursor format | no | stop processing for source and raise operator action required alert | checkpoint blocked | source adapter + ops |

Classification precedence rules:
- If multiple errors are observed for one operation, classify using the most specific stage-local code above.
- `ENVELOPE_INVALID` always takes precedence over transport-level publish errors.
- `PERSIST_CONSTRAINT` is non-retriable even if retried by driver-level middleware.
- `CHECKPOINT_DATA_INVALID` is always non-transient.

### 3.5.4 Retry Policy Matrix (v1)

This matrix is normative and defines required default retry parameters. Implementations may override via configuration per environment, but must preserve policy ids, formulas, and exhaustion actions.

Backoff formula used by exponential policies:

$$
delay_n = min(cap,\; base \cdot 2^{(n-1)}) \cdot jitter
$$

where `n` is the retry attempt number starting at `1`, and `jitter` is sampled from the policy jitter range.

| Policy ID | Applies To | Retries Error Codes | Max Attempts | Backoff | Jitter | Per-Attempt Timeout | Max Elapsed | On Exhaustion |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `source_fetch_transient` | inbound source adapter fetch operations | `SRC_TIMEOUT`, `SRC_RATE_LIMIT`, `SRC_UNAVAILABLE` | `5` | exponential (`base=1s`, `cap=30s`) | random in `[0.80, 1.20]` | `15s` | `120s` | mark source cycle failed, emit alert-level event, continue other sources |
| `persist_transient` | canonical-event and outbox transaction writes | `PERSIST_TRANSIENT` | `4` | exponential (`base=500ms`, `cap=8s`) | random in `[0.85, 1.15]` | `5s` | `30s` | fail batch as non-success, do not advance checkpoint |
| `publish_transient` | broker publish for one obligation | `PUBLISH_TRANSIENT` | `8` | exponential (`base=1s`, `cap=60s`) | random in `[0.80, 1.20]` | `10s` | `600s` | set `last_error_code=PUBLISH_RETRY_EXHAUSTED`, mark obligation `dead_lettered` |
| `checkpoint_cas_conflict` | checkpoint compare-and-set update | `CHECKPOINT_CAS_CONFLICT` | `6` | exponential (`base=100ms`, `cap=2s`) | random in `[0.90, 1.10]` | `2s` | `15s` | fail checkpoint update for batch, keep batch unresolved for recovery retry |
| `usage_accounting_transient` | optional usage-record write path | transient storage/network errors in usage accounting adapter | `3` | exponential (`base=1s`, `cap=10s`) | random in `[0.90, 1.10]` | `3s` | `20s` | drop usage write, increment `usage_write_drop_count`, continue pipeline |

Normative retry semantics:
- `Max Attempts` includes the initial attempt.
- A retry must not begin when `Max Elapsed` would be exceeded by waiting plus `Per-Attempt Timeout`.
- Retries must preserve idempotency inputs (`canonical_event_id`, `dedupe_key`, checkpoint cursor input).
- Retry state must be observable: attempt number, delay, timeout, and final disposition.

Dead-letter completion policy:
- Required configuration key: `dead_letter_completes_obligation`.
- Default value: `true`.
- When `true`, obligations terminally marked `dead_lettered` satisfy checkpoint gate conditions.
- When `false`, any `dead_lettered` obligation keeps the batch non-success and blocks checkpoint advancement.

Required configuration keys and defaults:
- `retry.source_fetch_transient.max_attempts=5`
- `retry.source_fetch_transient.base_delay_ms=1000`
- `retry.source_fetch_transient.max_delay_ms=30000`
- `retry.source_fetch_transient.attempt_timeout_ms=15000`
- `retry.source_fetch_transient.max_elapsed_ms=120000`
- `retry.persist_transient.max_attempts=4`
- `retry.persist_transient.base_delay_ms=500`
- `retry.persist_transient.max_delay_ms=8000`
- `retry.persist_transient.attempt_timeout_ms=5000`
- `retry.persist_transient.max_elapsed_ms=30000`
- `retry.publish_transient.max_attempts=8`
- `retry.publish_transient.base_delay_ms=1000`
- `retry.publish_transient.max_delay_ms=60000`
- `retry.publish_transient.attempt_timeout_ms=10000`
- `retry.publish_transient.max_elapsed_ms=600000`
- `retry.checkpoint_cas_conflict.max_attempts=6`
- `retry.checkpoint_cas_conflict.base_delay_ms=100`
- `retry.checkpoint_cas_conflict.max_delay_ms=2000`
- `retry.checkpoint_cas_conflict.attempt_timeout_ms=2000`
- `retry.checkpoint_cas_conflict.max_elapsed_ms=15000`
- `retry.usage_accounting_transient.max_attempts=3`
- `retry.usage_accounting_transient.base_delay_ms=1000`
- `retry.usage_accounting_transient.max_delay_ms=10000`
- `retry.usage_accounting_transient.attempt_timeout_ms=3000`
- `retry.usage_accounting_transient.max_elapsed_ms=20000`
- `outbox.claim.batch_size=100`
- `outbox.claim.lease_ms=30000`
- `outbox.claim.heartbeat_interval_ms=10000`
- `outbox.claim.reclaim_grace_ms=0`

### 3.5.5 Publication Routing Contract (v1)

This section is normative and defines the minimum routing rules required for deterministic publishing while preserving publisher-consumer decoupling.

Decoupling rule:
- Producers must route by event metadata (`event_type`, optional `partition_key`) and environment configuration only.
- Producers must not encode knowledge of specific consumer names, deployments, or subscriptions.

Destination mapping rule:
- For every emitted `event_type`, exactly one routing target must be resolved at publish time.
- Routing target resolution must be deterministic for identical inputs.
- Unknown or unmapped `event_type` is a non-transient publish configuration error and must not be silently dropped.

Partition/routing-key rule:
- If `partition_key` is present in the envelope, producer must pass it through unchanged to broker routing metadata.
- If `partition_key` is absent, producer must derive routing metadata from `dedupe_key`.
- Retries and replays must preserve the same effective routing metadata.

Environment override rule:
- Destination mapping may vary by deployment environment (`dev`, `staging`, `prod`) through configuration only.
- Environment overrides must not change envelope semantics (`event_type`, `dedupe_key`, payload identity).

Dead-letter routing rule:
- Non-transient publish validation failures must route to dead-letter handling with reason code.
- Transient publish failures that exhaust retry policy must route to dead-letter handling with exhaustion metadata.

Observability rule:
- Producer must log resolved routing target, effective routing metadata, and publish disposition per obligation.

Required configuration keys and defaults:
- `routing.default_target=news-events`
- `routing.event_type.news.article.created.target=${routing.default_target}`
- `routing.partition_key.mode=partition_key_or_dedupe_key`
- `routing.unmapped_event_type_action=error`
- `routing.environment_override_enabled=true`

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
