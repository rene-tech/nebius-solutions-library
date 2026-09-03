# Scientific batch controller

The `fs2_serve.scientific_batch` package is the reusable state-machine core for
staged scientific workloads and its production control-plane consumer. The
state machine remains isolated behind repository and Kubernetes protocols;
`PostgresScientificBatchRepository`, `HttpScientificBatchCluster`, the API/MCP
service, and the supervised worker implement those boundaries without making
the internal records into public schemas.

## Durable ownership and admission

Every batch is keyed by the UUID of an existing durable FS2 Operation. Migration
`0015_scientific_batch_controller.sql` stores only orchestration state, events,
and controller fencing; it does not create a second public operation ledger or
duplicate the artifact-service-owned attempt, artifact, stage-commit, or result
tables.
`batch_id` and logical `workload_id` are deterministic, stable children of the
Operation. Both survive retry; only `attempt_id` and the concrete Kubernetes
resource name change. The service projects these internal UUIDs to opaque
scientific API identities.

Admission writes an immutable internal `ScientificBatchPlan` and
`SchedulingSnapshot` once. The snapshot uses the Kueue scheduling contract's
terminology for resolved LocalQueue/ClusterQueue, priority class/value, ordered
pool preference, resource class, accelerator resource/count,
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

The resolver verifies the contract's caller-selectable flag, tenant/model
`local_queue_routes`, LocalQueue-to-ClusterQueue binding, WorkloadPriorityClass
value, ordered compatible pools, each pool's ResourceFlavor and extended
resource, queue deadline, execution deadline, and preemption mode. Queue expiry
is enforced from the frozen attempt start without retry; the execution deadline
is projected to both Kueue's max-exec label and the Job or JobSet Job template.
The frozen snapshot has no admitted ResourceFlavor field. The exact Kueue
admission—Workload UID plus actual pool, ResourceFlavor, accelerator resource,
quantity, and admission time—is stored only on the attempt after the live
Kueue Workload reports `Admitted`. A started, successful, or preempted workload
without that evidence cannot advance, and its actual resource/count must match
the frozen stage request.

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
negative semantic result cannot unlock downstream work. Kubernetes completion
may become visible before its artifact transaction; the controller first
persists the terminal attempt observation, then releases the completed Job and
waits for the commit instead of treating temporary absence as a scientific
failure.

## Workload mapping and quota handoff

- An `independent-jobs` stage creates one independently named Kubernetes `Job`
  per shard.
- A `gang-jobset` stage creates exactly one `JobSet` with `gang_size >= 2`.
  JobSet is never used as a fan-out convenience, and a gang is never degraded
  into unrelated Jobs.
- Workload names and attempt UUIDs are deterministic functions of Operation,
  stage, shard, and attempt number. Re-applying after a process crash therefore
  adopts the same object instead of duplicating work.
- Attempt identity and the active stage are committed before Kubernetes apply.
  The second fenced transition binds the returned Job/JobSet UID. A crash in
  between leaves a recognizable pending apply, and a fast Kueue admission can
  never present a companion capability before the controller record exists.
- A terminal observation is durable before workload deletion. The separate
  `resource_released` marker is persisted after an idempotent, UID-fenced
  delete, before the durable stage-success transition. If the controller loses
  its database write after deleting, its successor safely repeats deletion.
  This is the quota-handoff fence before the next sequential stage can be
  created.

Kubernetes implementations need not reconstruct an observation after deletion:
the terminal outcome is already durable. Apply and delete must reject an older
controller fence and verify immutable attempt ownership before adopting an
existing deterministic name.

## Attempts, failures, and cancellation

Each attempt has a stable UUID and increasing per-shard attempt number. An
observation whose attempt UUID does not equal the current resource binding is
ignored and emits the deduplicated `attempt_fenced` event. Only
`infrastructure` and `preemption` failures may be retried, and only below the
stage's admission-time attempt limit. User input, application, semantic
validation, and stale or negative commit bindings terminalize the batch.

Cancellations are durable level signals, so a cancellation racing with Job
creation is carried through the reconciler's next compare-and-swap rather than
orphaning the just-created object. Cancellation is a fenced cascade: all active
Jobs/JobSets receive idempotent deletes, active attempts move through
grace/drain and teardown, every non-succeeded stage becomes cancelled, and no
later stage can be created.

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
   monotonically increasing sequences.

Artifact publication is a separate atomic producer transaction. Readers see
either no `ArtifactCommit` or the complete manifest and semantic-validation
receipt. Credentials, raw inputs, raw outputs, and validator diagnostics do
not belong in lifecycle events.

## Production API, MCP, and artifact projection

The public adapters consume the canonical catalog schemas directly; there are
no controller-owned copies of the scientific request, result, profile, or
artifact-pointer JSON schemas.

- `POST /v1/models/{model_id}:submit` validates
  `scientific-run-request/v1`, authorizes the profile/model, freezes the
  scheduling decision at the durable Operation `accepted_at`, and returns 202.
- `GET /v1/operations/{operation_id}` returns the durable Operation and internal
  batch-status projection, including an optional canonical
  `scheduling_admission` on every attempt. It remains null before Kueue resolves
  admission. The status and event projections are bounded closed objects;
  `GET .../events` returns ordered stable lifecycle identities. `DELETE ...`
  and the existing `:cancel` route request the same idempotent cascade.
- `GET /v1/operations/{operation_id}/result` is reopened through the narrow
  artifact result port and validated against the existing
  `scientific-run-result/v1`. The controller does not define a second terminal
  result transport.
- `GET /v1/artifacts/{artifact_id}` returns only the canonical pointer
  projection. Storage keys, signed handles, access credentials, payload bytes,
  and internal artifact records are never returned.
- MCP exposes matching submit/status/cancel/events/artifact/result tools. Its
  submit path additionally requires the profile's canonical `mcp.invocable`
  gate; all tools reuse the HTTP service and never create Kubernetes objects
  directly.

The controller core depends only on `ScientificBatchArtifactLifecycle` and
`ScientificBatchResultPublisher`; the replaceable `ArtifactServiceBridge`
adapts those ports to the artifact service. The artifact service remains the
sole owner of uploads, immutable artifact records, per-attempt lifecycle,
stage commits, terminal result manifests, and their public projection. A
canonical terminal result must retain every terminal stage/shard attempt,
including preempted and retried attempts, under the existing
`scientific-run-result/v1` attempt list. Its stage-commit rows bind the exact
successful attempt set to an atomically synthesized aggregate manifest and
validation digest. A successful Job cannot unlock a successor until this
commit has been committed and reopened.

## Kubernetes and Helm wiring

`HttpScientificBatchCluster` uses the projected Kubernetes API token and the
operator-owned execution map to POST suspended Kueue-managed Jobs or JobSets.
It verifies deterministic name, attempt ownership, immutable manifest digest,
and live UID before adoption or UID-preconditioned deletion. The Kueue contract
is authoritative for LocalQueue, ClusterQueue, workload priority, accelerator,
pool order, and preemption. ResourceFlavor is absent from the submission
snapshot because only Kueue may choose it; it appears later only in the attempt
admission record.

The execution map is a closed operator configuration, not a public request or
catalog schema extension. Version 2 binds each public `model_id` to one exact
dynamic `variant_id`, one packaged plan adapter, and all of its profile stages.
The binding is frozen into durable controller state and propagated as the raw
`FS2_VARIANT_ID`, a bounded Kubernetes label, and an exact annotation. The
profile remains the authority for the execution-identity digest and image
digest; the Kueue scheduling contract remains the only authority for queue,
priority, pool, flavor, and accelerator fields.

Each model entry has exactly these fields:

```json
{
  "model_id": "protenix-v2",
  "variant_id": "protenix-v2-h100",
  "execution_identity_sha256": "<64 lowercase hex>",
  "plan_adapter": {
    "module": "fs2_serve.scientific_batch.adapters",
    "function": "compile_adapter_run"
  },
  "runtime_artifacts": [],
  "stages": []
}
```

The execution map may name only the controller-owned dispatcher shown above.
Model packages register their compiler and collectors through
`register_adapter(model_id=..., compiler=..., collectors=...)`; they never add
another manifest renderer, artifact client, or commit route. The stable
compiler call is:

```python
compiler(
    profile,
    request,
    *,
    operation_id: str,
    variant_id: str,
    access_context: ArtifactAccessContext,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> AdapterExecutionPlan
```

The returned internal `AdapterExecutionPlan` carries `model_id`, `variant_id`,
`source_revision`, canonical `request_sha256`, `controller_plan`, one
`StageInvocation` per expanded stage/shard, and `required_model_artifacts`.
Each invocation supplies exact non-shell `argv`, fixed non-secret environment,
contained working directory, logical `consumes` and one logical `produces`, an
`ArtifactMaterialization` for every consumed input, `collector_id`,
`validator_id`, downstream `handoff_name` where required, output count/byte
bounds, and the exact runtime artifact IDs it needs. The controller verifies
profile stage topology, variant/source identity, execution-map
collector/validator bindings, unique logical outputs, direct-predecessor
handoffs, and one canonical terminal output before admission.

A registered collector has the signature
`collector(invocation: StageInvocation, workspace: Path) -> CollectedStageOutput`.
It returns bounded `CollectedArtifactFile` records and a validation mapping
whose `validator_id` equals the invocation. The controller companion performs
all byte transport, immutable artifact creation, output-manifest/evidence
publication, and attempt close. After every shard is successful and quota has
been released, the controller asks the canonical artifact service to atomically
commit the exact attempt set for the stage. Model-owned code only locates and
semantically validates files beneath the supplied contained workspace.

Each execution-map stage has exactly `stage_id`, digest-qualified `image`,
`collector_id`, `validator_id`, `mounts`, `service_account_name`, `resources`,
`active_deadline_seconds`, `termination_grace_seconds`, and fixed
`environment`. Run-specific argv and materializations come only from the
admitted invocation. The renderer copies argv directly to container `command`;
there is no shell or request interpolation.

Every stage declares exactly one writable `artifact-workspace` at
`/mnt/fs2-scientific`. A controller-tools init container prepares it and one
init container per resolved logical artifact downloads the exact immutable
artifact, verifies digest, size, and media type, then performs the declared
contained copy/extract/overlay/input rewrite. The model gets only the resulting
paths. The fixed controller-tools collector sidecar calls the registered
collector after the model exits, validates bounds and semantic evidence,
uploads each output plus canonical manifest/evidence, and commits the exact
handoff artifacts. A successor is rendered only after the controller's atomic
stage commit is reopened and all artifact identities match its successful
predecessor attempt.

Additional `reference` and `private` mounts require an exact PVC claim, are
always read-only, and may use a safe relative `sub_path`. Every mount object has
exactly:

```json
{
  "name": "reference-data",
  "kind": "reference",
  "claim_name": "scientific-reference-data",
  "mount_path": "/run/fs2/reference",
  "sub_path": "protenix-v2",
  "read_only": true
}
```

For `artifact-workspace`, `claim_name` and `sub_path` are `null`. Caller values
can select only fields allowed by the public request/parameter schemas; they
never become image, collector, validator, queue, priority, flavor, mount,
claim, or URL values. Adapter-generated paths must remain under the contained
workspace root.

`runtime_artifacts` is an operator attestation, not a request field. Each
execution-map item binds a logical artifact ID and physical read-only source to
an exact aggregate content digest, complete `(path, sha256, size_bytes)` file
manifest, and localization receipt digest. Each `StageInvocation` lists the
runtime artifacts used by that enabled stage and carries one exact
`RuntimeArtifactMount` per ID: approved target `mount_path`, optional safe
`sub_path`, expected content SHA, authorization/readiness receipt SHAs, and
supplemental groups. The renderer exposes only those exact bindings, emits the
verified receipt document atomically at
`<working_directory>/.fs2/runtime-localization.json`, and requires that exact
path in model argv. Wrappers verify the small marker rather than rehashing a
multi-GB tree per attempt. The Pod security context sets
`fsGroupChangePolicy=OnRootMismatch` without setting `fsGroup`, so Kubernetes
does not recursively chown a multi-GB immutable tree.

Before a durable batch row or Kubernetes object exists, the controller verifies
every plan-required artifact and rejects any required ID undeclared by the
qualified profile. Profile artifacts for disabled stages may remain unused;
this is required for AlphaFold3 `input_mode=enriched`, where the CPU reference
stage is intentionally absent. Missing, partial, receipt-different, or
digest-different weights/databases fail admission before GPU scheduling.

The frozen `ArtifactAccessContext` carries the tenant, `public|restricted|academic`
profile, and non-secret receipt digest to every materializer/collector
capability. Thus an academic AlphaFold3 CPU preprocessing invocation can consume
the authorized raw input while its GPU successor consumes only one committed,
validated immutable handoff envelope containing relative `processed.json` and
its digest-bound provenance marker; raw input is not implicitly forwarded. The
same relative envelope contract is used for enriched input and survives
relocation into the GPU workspace—producer absolute paths are never trusted.
Protenix preprocessing uses the same relative-path rule, and both its CPU and
GPU invocations must declare the complete common bundle
`components.cif`, `components.cif.rdkit_mol.pkl`,
`clusters-by-entity-40.txt`, and `obsolete_release_date.csv`; a weights-only GPU
stage is invalid.

Helm's `scientificBatch.enabled` and `scientificBatch.writesEnabled` gates are
both false by default and must be enabled together. Enabling requires:

- immutable scheduling-contract and execution-map ConfigMaps;
- the canonical `scientificArtifacts.enabled` PostgreSQL/S3 service;
- bounded Kubernetes API egress CIDRs;
- Job, JobSet, and Pod read/write RBAC in the configured workload namespace;
- a catalog profile whose route, immutable execution identity, access state,
  semantic validator, and parameter schema are all qualified.

The control-plane Pod UID is the controller identity. Each replica runs fenced
workers over the shared PostgreSQL queue; generic inference workers explicitly
exclude `scientific-batch-v1` Operations. Every worker observes its configured
poll interval even while a batch remains continuously claimable, and readiness
fails after repeated errors in any worker. Current checked-in scientific
profiles remain candidate/unqualified, so the feature cannot be enabled
truthfully until a qualified adapter lane lands its exact profiles, execution
map, and scheduling ConfigMap.

## Verification

The deterministic fake repository enforces compare-and-swap revisions,
immutable admission, monotonically sequenced events, and controller fences.
The fake cluster enforces immutable names and mutation fences. The focused
tests cover sequential commit gating, contained logical-manifest resolution,
relative AF3/Protenix handoff relocation, exact runtime mounts and localization
receipts, delayed artifact publication, fan-out and gang rendering, quota
handoff (including an injected crash after external deletion), canonical
scheduling routing/deadlines, complete phase ingestion, infrastructure
preemption/retry, stale-attempt observations, non-retryable taxonomy,
cancellation races and cascade, public HTTP/MCP lifecycle dispatch, Kubernetes
REST creation, unresolved-versus-actual Kueue admission, closed API transports,
durable PostgreSQL fencing, and invalid DAGs/snapshots.

Run them with:

```bash
cd k8s-inference/components/control-plane
PYTHONPATH="src:../../catalog/runtime" uv run pytest -q \
  tests/test_scientific_batch_controller.py \
  tests/test_scientific_batch_catalog_adapter.py \
  tests/test_scientific_batch_production.py \
  tests/test_scientific_batch_execution_handoff.py \
  tests/test_scientific_artifacts.py

# Requires a disposable PostgreSQL database.
FS2_TEST_DATABASE_URL=postgresql://... uv run pytest -q \
  tests/test_postgres_integration.py -k scientific_batch

# Includes schema-valid test values, Helm lint/template, static checks, and the full suite.
bash scripts/test.sh
```
