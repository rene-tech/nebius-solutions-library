# Scientific batch controller core

The `fs2_serve.scientific_batch` package is the reusable state-machine core for
staged scientific workloads. It is deliberately not wired into the API,
PostgreSQL, Kubernetes, Kueue, Helm, or model adapters yet. Those integrations
can implement the two protocols in `protocols.py` without changing controller
semantics.

## Durable ownership and admission

Every batch is keyed by the UUID of an existing durable FS2 Operation. The
future PostgreSQL implementation must retain that parent relationship; the
batch repository does not create a second public operation ledger.
`batch_id` and logical `workload_id` are deterministic, stable children of the
Operation. Both survive retry; only `attempt_id` and the concrete Kubernetes
resource name change. A later persistence adapter projects these internal UUIDs
to the corresponding opaque scientific API identities.

Admission writes an immutable internal `ScientificBatchPlan` and
`SchedulingSnapshot` once. The snapshot uses the Kueue scheduling contract's
terminology for resolved LocalQueue/ClusterQueue, priority class/value, ordered
pool preference, admitted ResourceFlavor, accelerator resource/count,
queue/execution ceilings, checkpoint mode, and preemption mode. It also freezes
one of `presentation`, `interactive`, `customer-batch`, or `bulk-backfill`, plus
the logical tenant queue, model lane, policy revision, and capture time. The
canonical digest makes drift visible. An idempotent replay may return the
existing batch only when tenant, internal plan, and snapshot are byte-for-byte
equivalent. A later policy or capacity change never changes an admitted batch.

The scheduling types are an internal frozen consumption model, not a competing
Kueue policy authority. Integration must project the reviewed Kueue scheduling
contract into these fields; this controller does not choose queues, priorities,
flavors, images, commands, or paths.

## Catalog profile adapter boundary

`scientific_plan_from_catalog_profile` consumes only the `workload` projection
of an already schema-validated canonical scientific workload profile. It maps
catalog stage IDs, dependencies, resource classes, admission modes,
parallelism bounds, checkpoint/preemption modes, and retry ceiling into
distinctly named internal `ScientificStagePlan` records. Run-specific shard or
gang expansion is bounded by the catalog minimum/maximum.

This adapter is intentionally not the public scientific JSON schema and does
not validate or redefine it. The catalog consumer/schema owner remains the
authority. Caller-controlled argv, image, path, queue, priority, flavor, or URL
fields never enter the projection. Catalog `retryable_exit_codes` also do not
bypass the controller taxonomy: only an adapter-classified infrastructure or
preemption failure is retryable.

Only one DAG stage is active at a time. A stage becomes eligible after every
predecessor is durably succeeded and its atomic artifact-manifest commit is
reopened, bound to the exact successful attempt IDs, and marked semantically
valid. A partial upload, stale-attempt commit, missing validation receipt, or
negative semantic result cannot unlock downstream work.

## Workload mapping and quota handoff

- An `independent-jobs` stage creates one independently named Kubernetes `Job`
  per shard.
- A `gang-jobset` stage creates exactly one `JobSet` with `gang_size >= 2`.
  JobSet is never used as a fan-out convenience, and a gang is never degraded
  into unrelated Jobs.
- Workload names and attempt UUIDs are deterministic functions of Operation,
  stage, shard, and attempt number. Re-applying after a process crash therefore
  adopts the same object instead of duplicating work.
- Terminal stage resources are deleted before the durable stage-success
  transition. Kueue also releases admission for terminal Jobs; the explicit
  idempotent delete is the cleanup and quota-handoff fence before the next
  sequential stage can be created.

Kubernetes implementations must retain or reconstruct terminal observations
long enough for reconciliation after an idempotent delete. Apply and delete
must reject an older controller fence and must verify immutable attempt
ownership before adopting an existing deterministic name.

## Attempts, failures, and cancellation

Each attempt has a stable UUID and increasing per-shard attempt number. An
observation whose attempt UUID does not equal the current resource binding is
ignored and emits the deduplicated `attempt_fenced` event. Only
`infrastructure` and `preemption` failures may be retried, and only below the
stage's admission-time attempt limit. User input, application, semantic
validation, missing commits, and stale commit bindings terminalize the batch.

Cancellation is a fenced cascade: all active Jobs/JobSets receive idempotent
deletes, active attempts move through grace/drain and teardown, every
non-succeeded stage becomes cancelled, and no later stage can be created.

## Lifecycle evidence

Lifecycle markers are monotonic within an attempt. A retry starts a new
identity rather than moving an earlier attempt backwards:

1. `queued`
2. `scheduling`
3. `admitted`
4. `node_pending`
5. `image_loading`
6. `artifact_loading`
7. `restoring`
8. `semantic_warmup`
9. `active_compute`
10. `allocated_idle`
11. `grace_drain`
12. `preempted` when applicable
13. `teardown`

Not every runtime uses every phase (for example, a normal cold start skips
`restoring`). The cluster adapter supplies the observed ordered phase history;
the core appends only forward markers. Every event has a deterministic SHA-256
identity over Operation, stage, shard, attempt, phase, kind, and bounded code.
The repository assigns one increasing sequence per Operation and deduplicates
by event identity, so reconcile replay cannot double count a phase.

## Repository transaction contract

`ScientificBatchRepository.replace` is the single durable write boundary. A
production adapter must, in one transaction:

1. verify the current controller claim and unexpired fencing token;
2. compare the expected batch revision;
3. prove the immutable plan and scheduling snapshot did not change;
4. replace orchestration state at exactly the next revision; and
5. append new lifecycle/controller events, deduplicated by `event_id`, with
   gap-free monotonically increasing per-Operation sequences.

Artifact publication is a separate atomic producer transaction. Readers see
either no `ArtifactCommit` or the complete manifest and semantic-validation
receipt. Credentials, raw inputs, raw outputs, and validator diagnostics do
not belong in lifecycle events.

## Verification

The deterministic fake repository enforces compare-and-swap revisions,
immutable admission, monotonically sequenced events, and controller fences.
The fake cluster enforces immutable names and mutation fences. The focused
tests cover sequential commit gating, fan-out and gang rendering, quota
handoff, complete phase ingestion, infrastructure preemption/retry,
stale-attempt observations, non-retryable taxonomy, cancellation cascade, and
invalid DAGs/snapshots.

Run them with:

```bash
cd k8s-inference/components/control-plane
PYTHONPATH="src:../../catalog/runtime" uv run pytest -q \
  tests/test_scientific_batch_controller.py \
  tests/test_scientific_batch_catalog_adapter.py
```
