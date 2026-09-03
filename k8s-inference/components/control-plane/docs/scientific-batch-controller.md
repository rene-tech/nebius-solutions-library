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
terminology for execution namespace, resolved LocalQueue/ClusterQueue, priority class/value, ordered
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

The resolver verifies the contract's caller-selectable flag, tenant/model
`local_queue_routes`, LocalQueue-to-ClusterQueue binding, WorkloadPriorityClass
value, ordered compatible pools, each pool's ResourceFlavor and extended
resource, queue deadline, execution deadline, and preemption mode. Queue expiry
is enforced from the frozen attempt start without retry; the execution deadline
is projected to both Kueue's max-exec label and the Job or JobSet Job template.
The exact Kueue admission, Workload UID, admitted flavor, pool, and accelerator
quantity are then persisted from live status rather than guessed at submission.

Controller startup additionally projects every deployment-owned
`(model, stage) -> (namespace, LocalQueue, ClusterQueue)` target from the
immutable execution map and requires an exact matching LocalQueue and
`local_queue_routes` entry in the mounted immutable scheduling ConfigMap. A
LocalQueue that merely exists in Kubernetes is insufficient. In particular,
the academic-assets companion creates `fs2-academic-poc/academic-scientific`,
but the Terraform integration owner must also project that external queue and
its `alphafold3` route to `inference-accelerators` into
`module.kueue_scheduling.contract.local_queues` and
`.local_queue_routes`, then publish the resulting content-addressed immutable
ConfigMap. Until that happens, scientific-controller startup/freeze fails
closed and the AF3 route is not deployable.

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

The bounded state codec writes the current v9 document and can reopen exact v6
and v7 documents from before trusted multi-namespace execution was introduced.
A v6 invocation is recovered with no namespace/LocalQueue override and uses the
legacy `fs2-models` execution namespace; a v7 invocation retains its persisted
namespace/LocalQueue pair, including the academic lane. The next successful
repository replacement rewrites either legacy form as strict v9. Unknown or
mixed version shapes remain rejected, and both reads and writes retain the
4 MiB state limit.

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
  batch-status projection. Every stage reports the execution namespace and
  resolved LocalQueue frozen at admission; every attempt reports its persisted
  workload namespace alongside kind, name, and UID. Kubernetes workload names
  are namespace-scoped, so operators must use the returned namespace/name pair
  when locating a Job or JobSet. `GET .../events` returns ordered stable
  lifecycle identities. `DELETE ...` and the existing `:cancel` route request
  the same idempotent cascade.
- `GET /v1/operations/{operation_id}/result` is assembled by
  `ArtifactServiceBridge` from the artifact service's immutable terminal
  manifest and validated against `scientific-run-result/v1`.
- `GET /v1/artifacts/{artifact_id}` returns only the canonical pointer
  projection. Storage keys, signed handles, access credentials, payload bytes,
  and internal artifact records are never returned.
- MCP exposes matching submit/status/cancel/events/artifact/result tools. Its
  submit path additionally requires the profile's canonical `mcp.invocable`
  gate; all tools reuse the HTTP service and never create Kubernetes objects
  directly.

The artifact service remains the sole owner of uploads, immutable artifact
records, per-attempt lifecycle, stage commits, terminal result manifests, and
their public projection. Its stage-commit rows bind the exact successful
attempt set to an atomically synthesized aggregate manifest and validation
digest. A successful Job cannot unlock a successor until this commit has been
committed and reopened.

## Kubernetes and Helm wiring

`HttpScientificBatchCluster` uses the projected Kubernetes API token and the
operator-owned execution map to POST suspended Kueue-managed Jobs or JobSets.
It verifies deterministic name, attempt ownership, immutable manifest digest,
and live UID before adoption or UID-preconditioned deletion. The Kueue contract
is authoritative for LocalQueue, ClusterQueue, workload priority, accelerator,
pool order, and preemption. ResourceFlavor remains null at submission because
only Kueue may choose it.

The execution map is a closed operator configuration, not a public request or
catalog schema extension. Version 3 binds each public `model_id` to one exact
dynamic `variant_id`, one packaged plan adapter, and all of its profile stages.
The binding is frozen into durable controller state and propagated as the raw
`FS2_VARIANT_ID`, a bounded Kubernetes label, and an exact annotation. The
profile remains the authority for the execution-identity digest and image
digest. Each stage also binds a trusted namespace and LocalQueue; the resolver
must prove that pair exists in the Kueue scheduling contract before freezing
the queue, priority, pool, flavor, and accelerator fields. A request cannot
select or override either value.

The trusted execution namespace is part of every stage scheduling decision and
therefore of the immutable snapshot digest. The controller persists attempt
`WorkloadRef` objects from that decision, checks the bound `StageInvocation`
namespace/LocalQueue pair for exact equality, and uses the persisted reference
for apply, observe, and delete. Job and JobSet metadata and every Pod template
carry the same resolved Kueue queue label. Unbound/default plans must remain in
the controller's configured `fs2-models` namespace.

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

Each execution-map stage has exactly `stage_id`, `execution_namespace`,
`local_queue_name`, digest-qualified `image`,
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

Additional `reference` and `private` mounts require an operator-owned PVC in
the exact execution namespace, are always read-only, and may use a safe
relative `sub_path`. Every mount object has
exactly:

```json
{
  "name": "reference-data",
  "kind": "reference",
  "artifact_id": "protenix-v2",
  "claim_name": "scientific-reference-data",
  "claim_namespace": "fs2-models",
  "host_path": null,
  "operator_owned": true,
  "mount_path": "/run/fs2/reference",
  "sub_path": "protenix-v2",
  "read_only": true
}
```

For `artifact-workspace`, every source field is `null` and `operator_owned` is
false. The only supported hostPath volume source is the operator-owned,
read-only AF3 public database root `/mnt/fs2-reference-data/data`, for model
`alphafold3`, stage `data-pipeline`, artifact
`alphafold3-public-databases-v3.0`. The root itself is never exposed at
`/databases`: a promoted aggregate-tree localization must pin the safe relative
subPath
`datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/<tree_sha256>`.
The renderer mounts only that subdirectory at `/databases`, requires its exact
canonical `file:///mnt/fs2-reference-data/data/...` URI and
`.fs2-manifest-sha256`. Node-accessibility evidence must bind the exact
`storage.fs2.nebius/reference-data=true` selector and cannot pin live node IDs.
The renderer also
requires that selector in the trusted stage contract again at apply time, so
aggregate evidence cannot repair an unsafe execution-map drift. It is conjoined
with normal Kueue ResourceFlavor accelerator placement without node or pool IDs,
and the renderer retains the
`dedicated=fs2-inference:NoSchedule` toleration. The data stage receives only
supplemental group `1000`, which can traverse the operator filesystem root
without changing ownership or recursively applying an `fsGroup`.
Adapters leave supplemental groups empty: the renderer binds exactly `1000` for
the AF3 database stage and `65532` for parameter inference from the single
matching deployment-owned physical source. Adapter, request and profile values
cannot supply or override these groups. Pod security uses strict supplemental
groups and never sets `fsGroup` or `fsGroupChangePolicy`.
The renderer compares the full tuple to a compiled allowlist; no request path
reaches a Kubernetes hostPath. A PVC whose declared namespace differs from the
execution namespace is rejected before admission.

The intended academic target is namespace `fs2-academic-poc`, workload
ServiceAccount `fs2-academic-runner`, and LocalQueue `academic-scientific`
routing to the shared `inference-accelerators` ClusterQueue. The controller
caller is the distinct ServiceAccount
`fs2-system/fs2-serve-control-plane-runtime`. The execution map repeats that
trusted subject and the renderer fails closed if it differs from runtime
deployment configuration. Those namespaced resources and cross-namespace
control-plane RBAC are deployment prerequisites owned by the academic-assets
infrastructure module, not objects created by this controller or from a
scientific request. The companion's LocalQueue resource does not itself update
the immutable scheduling contract; that external-queue projection remains a
required Terraform integration gate as described above. The parameter file is
the exact 1,020,545,840-byte
`alphafold3/af3.bin.zst` on the `academic-assets-runtime-rwx` RWX claim, mounted
read-only at `/models/af3.bin.zst` with supplemental group `65532`.

Three GPU stages have tightly allowlisted deployment-owned writable compiler
caches: AlphaFold3 `inference` at `/cache/alphafold3`, OpenFold3 `inference` at
`/cache/openfold3`, and Protenix `sample-structure` at `/cache/protenix`.
AlphaFold3 fixes `FS2_AF3_CACHE_ROOT` plus its JAX, Triton, and XDG child paths;
OpenFold3 fixes `TRITON_CACHE_DIR`, `TORCH_EXTENSIONS_DIR`, and `XDG_CACHE_HOME`;
Protenix additionally fixes `CUEQ_TRITON_CACHE_DIR`. Each cache is one
operator-owned, writable, same-namespace PVC with no artifact ID and no
subPath. The execution map overrides adapter environment values for these
fixed paths and rejects a cache on every other model/stage identity. These
bindings are auxiliary L1+ compiler/JIT optimizations, not L2: profile ceilings
are `L1`, qualified levels and routes remain Off/candidate-unqualified until
measured first-compile versus warm-compile H100 evidence exists. L2 requires an
actual GPU/process snapshot on shared filesystem or object storage, L3 a
local-disk snapshot, and L4 host-RAM residency. Caller values
can select only fields allowed by the public request/parameter schemas; they
never become image, collector, validator, queue, priority, flavor, mount,
claim, or URL values. Adapter-generated paths must remain under the contained
workspace root.

`runtime_artifacts` is an operator attestation, not a request field. Small
artifacts bind a logical artifact ID and physical read-only source to an exact
content digest, complete bounded `(path, sha256, size_bytes)` file manifest,
and localization receipt digest. Huge shared trees use a distinct bounded
aggregate form with no truncated file list: tree/content SHA, independent
manifest SHA, exact relative dataset path and URI, total file count, and a
node-accessibility receipt plus required selector (and optional bounded node
names). The AF3 form accepts trees
larger than 4096 files while the execution map and durable state remain under
their 4 MiB limits. Each `StageInvocation` lists the
runtime artifacts used by that enabled stage and carries one exact
`RuntimeArtifactMount` per ID: approved target `mount_path`, optional safe
`sub_path`, expected content SHA, optional canonical localization-manifest SHA,
authorization/readiness receipt SHAs, and supplemental groups. The renderer exposes only those exact bindings, emits the
verified receipt document atomically at
`<working_directory>/.fs2/runtime-localization.json`, and requires that exact
path in model argv. The AF3 wrapper additionally verifies the exact
`/databases/.fs2-manifest-sha256` from the pinned tree instead of rehashing or
enumerating the shared filesystem per attempt. The Pod security context sets
`fsGroupChangePolicy=OnRootMismatch` without setting `fsGroup`, so Kubernetes
does not recursively chown a multi-GB immutable tree.

Before a durable batch row or Kubernetes object exists, the controller verifies
every plan-required artifact and rejects any required ID undeclared by the
qualified profile. Profile artifacts for disabled stages may remain unused;
this is required for AlphaFold3 `input_mode=enriched`, where the CPU reference
stage is intentionally absent. Missing, partial, receipt-different, or
digest-different weights/databases fail admission before GPU scheduling.

The frozen `ArtifactAccessContext` carries access evidence for the caller's
input and output artifacts to materializer/collector capabilities. It is not
compared with the model's deployment license metadata. Academic/non-commercial
AlphaFold3 authorization, immutable parameter provenance, and readiness are
operator-owned deployment gates; an ordinary authorized PoC request does not
need a model-license receipt or a special tenant ID. Thus AlphaFold3 CPU preprocessing can consume
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
- Job, JobSet, and Pod read/write RBAC in every trusted execution namespace;
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
REST creation, durable PostgreSQL fencing, and invalid DAGs/snapshots.

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
