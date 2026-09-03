# Scientific batch controller

See [Scientific profile discovery and startup canary](scientific-profile-discovery.md)
for the fail-closed qualified-profile listing and zero-public-profile startup
contract.

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
Migration `0016_scientific_batch_state_v7.sql` first permitted a rolling read of
v6 rows. Migration `0017_scientific_batch_state_v8.sql` now preserves both v6
and v7 rows and upgrades them on their next controller write. The closed codec
derives the former single execution/LocalQueue namespace from an applied
attempt, or from the historical `fs2-models` default when no attempt exists;
every emitted state is v8 with both namespaces explicit. The migrations
normalize the exact historical v6 scheduling-stage representation (removing
the provisional admitted-flavor slot and deriving `resource_class` from the
immutable plan) and adds JSON null for the formerly absent runtime-mount
manifest identity. V8 additionally freezes the execution-map content digest,
per-stage image/resource bindings, and bounded aggregate-tree artifact identity
without rewriting legacy bytes during the trigger comparison. Frozen `ec3440a2`
v6 and `545d71d9` v7 codec fixtures are exercised through real PostgreSQL
`replace` and `request_cancel` writes; queue, admission, execution-map, or
runtime-manifest drift remains rejected.
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
the execution `workload_namespace`, Kueue `route_namespace`, logical tenant
queue, model lane, policy revision, and capture time. The two namespaces must
match because a Kueue LocalQueue and its Job are namespace-scoped, but the
controller supports multiple frozen namespaces and never silently posts all
routed work into one process default. The canonical digest makes drift visible. An
idempotent replay may return the existing batch only when tenant, internal
plan, and snapshot are byte-for-byte equivalent. A later policy or capacity
change never changes an admitted batch.

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
admission assignment—Workload UID plus actual pool, ResourceFlavor,
accelerator resource, quantity, and Kueue transition time—is stored once the
live Kueue Workload reports `QuotaReserved`. The lifecycle does not emit
`admitted` until the separate `Admitted=True` condition exists. This preserves
the exact same-poll assignment on queue timeout without falsely claiming the
Pod ran. A started, successful, or preempted workload without an actual
`Admitted` condition cannot advance, and its actual resource/count must match
the frozen stage request.

Kueue eviction is also an immutable-attempt boundary. After the controller has
durably recorded a `QuotaReserved` assignment, disappearance of that
assignment, a changed assignment on the same Workload, or recreation of the
Kueue Workload is recorded as preemption. The controller retains the original
ClusterQueue from the frozen stage decision and the original
pool/ResourceFlavor/resource/count/time assignment on the attempt, deletes the
UID-fenced Job or JobSet, waits for confirmed absence, and only then creates a
new attempt. It never accepts same-Workload automatic requeue as a continuation,
so the new attempt receives a new identity and a fresh queue deadline.
Raw Kueue `DeactivationTarget` causes and their later
`DeactivatedDueTo<Cause>`/`EvictedDueToDeactivatedDueTo<Cause>` forms are
normalized identically. `MaximumExecutionTimeExceeded` is the terminal
application/policy code `EXECUTION_TIMEOUT`, while `RequeuingLimitExceeded`
is an infrastructure failure eligible for the normal bounded new-attempt
retry. A generic `Deactivated` condition never replaces either exact cause.

For a mixed JobSet, reservation parsing is scoped to the stage's exact frozen
accelerator resource. CPU-only coordinator PodSets and unrelated extended
resources do not contribute to the GPU quantity and need no GPU ResourceFlavor.
Every PodSet that does request the exact GPU resource must carry a positive
quantity and one consistent ResourceFlavor; their total must equal the frozen
accelerator count before the assignment is persisted.

Routes are filtered by service class, model, and tenant. A matching exact tenant
route outranks a wildcard-tenant route; the explicitly unrestricted service-class
default is used only when no selected route remains. Exact and wildcard routes
may coexist, but two routes at the same rank are rejected. A namespace-bound
model that would fall through outside its licensed-asset namespace, any selector
mismatch, or an execution-map namespace different from the selected LocalQueue
namespace fails before durable batch admission. The execution map contributes
only the model's operator-owned workload namespace; it cannot supply or override
the LocalQueue, ClusterQueue, priority, pool, flavor, or resource fields.

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
- A terminal observation is durable before workload deletion. Kubernetes
  `DELETE 202` only advances the attempt to a durable `deletion_requested`
  state. A later reconcile performs a UID-specific GET; only `404` advances
  `resource_released` and emits `teardown`. A still-present UID retains GPU and
  quota accounting, while a changed UID or DELETE precondition `409` is a
  fenced conflict. If the controller loses its database write after deletion,
  its successor safely repeats the UID-preconditioned request. Confirmed
  absence is the quota-handoff fence before retry or the next sequential stage.

Kubernetes implementations need not reconstruct a full workload observation
after deletion: the terminal outcome is already durable. They must expose a
bounded UID-specific absence check. Apply and delete reject an older controller
fence and verify immutable attempt ownership before adopting or removing an
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
grace/drain, and the batch remains nonterminal while any exact UID is still
present. Only confirmed absence emits teardown and permits every non-succeeded
stage to become cancelled; no later stage can be created.

## Model and collector exit handshake

A staged Pod runs exactly two regular containers: `scientific-stage`, the model
itself, and `artifact-collector`, which publishes the model's output. Under
`restartPolicy: Never` Kubernetes settles a Pod only once *every* container has
terminated, so a model that dies without publishing a result used to leave the
collector polling for output that could never arrive. The Job then held its
admitted GPU reservation until `activeDeadlineSeconds` expired, which on a
long-running scientific stage is hours of wasted H100 capacity.

Two bounds close that gap.

The observer settles the attempt. `observe` already lists the attempt's Pods
and reads their container statuses, so it now also resolves an outstanding
collection directly from them:

- The model terminated non-zero while the collector is still running. The
  attempt fails immediately, classified by the same `_failure` taxonomy the
  Job-failure path applies to the same reasons. An `OOMKilled` stage stays a
  non-retryable application failure and a `Preempted` stage stays a retryable
  preemption, so the verdict is exactly the one the deadline would have
  produced, only much earlier.
- The model terminated successfully but the collector has not finished within
  `COLLECTION_GRACE_SECONDS` of the model's own `finishedAt`. The attempt fails
  with `collection_deadline_exceeded` as a non-retryable application failure,
  because a stage that published nothing collectable is its own result and must
  not re-queue. This grace is deliberately generous, since expiring it kills a
  collection that may still be uploading a large artifact; it only has to be
  far shorter than a scientific stage's multi-hour active deadline. A stage
  whose model exited non-zero gets no grace at all.

Both verdicts are ordinary terminal observations, so the existing reconciler
path deletes the workload and releases quota with no new owner, scheduler, or
public schema. The handshake stays out of the way whenever the model is still
running or both containers have already terminated.

The collector also bounds itself. The renderer passes
`--collection-deadline-seconds`, derived from the stage's active deadline less
`COLLECTION_DEADLINE_MARGIN_SECONDS`, and the poll loop raises
`CollectionDeadlineError` at that bound instead of sleeping forever. The
collector therefore exits non-zero and yields a deterministic controller
failure code even if the controller is unavailable, rather than being killed
opaquely by `DeadlineExceeded`.

The Pod's container statuses are used as the termination marker rather than a
supervisor wrapped around the model's argv, because wrapping regresses exactly
the cases this handshake exists to catch. A wrapper becomes PID 1, so it
absorbs the SIGTERM that drains a preempted stage; the container's own exit
code and `state.terminated.reason` become the wrapper's, destroying the
`OOMKilled` attribution the taxonomy depends on; and the third-party scientific
runtime images contain no trusted supervisor binary to run.

The out-of-memory case was measured rather than assumed. On a cgroup v2 node a
probe container ran `sh -c "<allocate past the limit>; echo WRAPPER-SURVIVED"`
under a 64Mi limit and ended `OOMKilled` with exit code 137 and **no log output
at all**: `memory.oom.group` had killed the shell alongside the allocation, so
the wrapper never reached its marker. A marker-writing supervisor is therefore
silent for precisely the failure it would be introduced to report, while the
Pod's container status still carries the exact `OOMKilled` reason.

A terminal cascade keeps stepping under the same claim until the batch stops
moving, bounded by `CASCADE_STEP_BOUND`. A cascade needs at least two durable
writes — one to record that deletes were requested, one to conclude that
Kubernetes released the resources — and returning to the poll loop between them
would report a cancelled batch, and its public Operation row, as running until
an unrelated later poll. Each step is still its own fenced compare-and-swap, so
a controller that dies mid-cascade resumes from what was committed; a cascade
that a slow cluster leaves unfinished is picked up by the next poll rather than
holding the lease.

## Legacy state rows

Stored state carries its schema version, and the codec reopens `v6` and `v7`
rows into the current record so historical batches stay readable. A pre-`v8`
row has no placement class, no raw scheduling contract digest, and no
execution-map or stage bindings, because those values did not exist when it was
written; the codec reopens them as the explicit null/empty representation.

Such an admission is readable but not executable: the controller can neither
render its workloads nor prove which image and resources it was admitted
against. A still-open legacy row is therefore retired rather than resumed. It
terminalizes as `failed` with `legacy_state_incompatible` and the retryable
`infrastructure` failure kind, so the caller may resubmit the request against
the current schema. Any workload identity the row carries is still deleted
through the same UID-fenced cleanup. Scientific batch has never been enabled on
a live cluster, so no such row can own real GPU capacity. A legacy row that
already reached a terminal status is left untouched and stays readable at its
original schema version.

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

- `POST /v1/scientific-artifacts/uploads` creates an idempotent, server-owned
  upload Operation and returns a write-once handle; finalization verifies the
  bytes and publishes the immutable pointer. Customers upload their input
  objects and then a canonical manifest that names those pointers before
  submitting a run. `GET /v1/artifacts/{artifact_id}/download` issues a
  tenant-authorized, short-lived read handle.
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
- MCP exposes matching upload/finalize/download and
  submit/status/cancel/events/artifact/result tools. Its submit path
  additionally requires the profile's canonical `mcp.invocable` gate; all
  tools reuse the same services and never create Kubernetes objects directly.
- `build_runtime` always constructs the authenticated scientific admin read
  service over the durable Operation/controller rows, canonical catalog
  receipts, and optional artifact result port. The admin BFF routes expose
  bounded run lists/details and model readiness without signed handles,
  payloads, storage keys, or guessed GPU time. Per-attempt Kueue Workload, Job,
  Pod, pool, ResourceFlavor, accelerator, and admission evidence is projected
  only when it exists in the durable controller/result records.

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

Every rendered GPU Pod template also receives required node affinity on
`accelerator.fs2.nebius/pool-id` for the frozen ordered pool preference. The
controller injects that constraint into every existing required node-selector
term, so a ClusterQueue that exposes several ResourceFlavors cannot admit the
workload onto an incompatible pool. CPU stages do not receive this GPU
constraint.

The execution map is a closed operator configuration, not a public request or
catalog schema extension. Version 3 binds each public `model_id` to one exact
dynamic `variant_id`, one execution namespace, one packaged plan adapter, and
all of its profile stages.
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
  "workload_namespace": "fs2-models",
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
`environment`, plus `required_node_labels`. Run-specific argv and materializations come only from the
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

Additional `reference` and `private` mounts require exactly one exact PVC or
allow-listed operator host path, are always read-only, and may use a safe
relative `sub_path`. Every mount object has
exactly:

```json
{
  "name": "reference-data",
  "kind": "reference",
  "claim_name": "scientific-reference-data",
  "host_path": null,
  "mount_path": "/run/fs2/reference",
  "sub_path": "protenix-v2",
  "read_only": true
}
```

For `artifact-workspace`, `claim_name`, `host_path`, and `sub_path` are `null`.
The only host-path source is the published reference-data dataset root
`/mnt/fs2-reference-data/data/datasets`; it requires a relative
`<bundle>/<revision>/sha256/<tree-sha256>` subpath and the node selector
`storage.fs2.nebius/reference-data=true`. A mutable alias or broad data root is
rejected before a Job exists. Caller values
can select only fields allowed by the public request/parameter schemas; they
never become image, collector, validator, queue, priority, flavor, mount,
claim, or URL values. Adapter-generated paths must remain under the contained
workspace root.

`runtime_artifacts` is an operator attestation, not a request field. Each
execution-map item binds a logical artifact ID and physical read-only source to
an exact content digest and localization receipt. Small artifacts use a bounded
`(path, sha256, size_bytes)` file manifest. Large installed trees use the v8
aggregate identity `(algorithm, tree digest, total bytes, file count, marker
path)`; this represents the observed 8,697-file PyRosetta tree without exceeding
the 4,096-entry bound. Each `StageInvocation` lists the
runtime artifacts used by that enabled stage and carries one or more exact
`RuntimeArtifactMount` projections per ID: approved target `mount_path`, optional safe
`sub_path`, expected content SHA, authorization/readiness receipt SHAs, and
supplemental groups. Repeated IDs are allowed only to project the same verified
identity into distinct target paths; every target is unique and every
projection carries the same controller-bound content and receipt evidence. The
renderer exposes only those exact bindings, emits the
verified receipt document atomically at
`<working_directory>/.fs2/runtime-localization.json`, and requires that exact
path in model argv. Wrappers verify the small marker rather than rehashing a
multi-GB tree per attempt. Published trees are pre-owned and mounted with the
declared `supplementalGroups`; the Pod does not set `fsGroup`, so kubelet does
not recursively chown a multi-GB immutable tree. If a future storage contract
requires `fsGroup`, it must also render `fsGroupChangePolicy=OnRootMismatch`.

Model compilers declare immutable mount intent and may leave the live readiness
receipt unset. They are not allowed to invent localization evidence. After the
execution map verifies every content/file manifest and authorization receipt,
the trusted controller binding injects the exact localization receipt into a
new frozen `AdapterExecutionPlan` before admission. Kubernetes apply rejects
any plan that has not passed that controller-owned binding step.

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

Native BindCraft is additionally fenced to the exact digest-qualified image
selected by the runtime catalog and the Terraform academic namespace,
ServiceAccount, PVC, and GID handoff. The admitted image digest is persisted and
injected as `FS2_RUNTIME_IMAGE_DIGEST`; it is never a hard-coded controller
constant. Its AF2
artifact must contain `manifest.json` and mount at `/models/alphafold2`; the
PyRosetta installed tree must have content identity
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`
and mount at `/opt/fs2/academic/pyrosetta-bindcraft/site-packages`. One verified
`bindcraft-proteinmpnn-weights` artifact is explicitly projected from its
`vanilla_model_weights` and `soluble_model_weights` manifest subtrees into the
two exact ColabDesign package directories. BindCraft argv must begin with
`python /opt/fs2/runtime_entrypoint.py`, so Kubernetes command override cannot
bypass the image's AF2 artifact gate, and the merged environment must remain
offline.

Native AlphaFold3 follows the Terraform academic handoff exactly: namespace,
GPU LocalQueue/ClusterQueue, reference-data LocalQueue/ClusterQueue, runner
ServiceAccount, private claim, supplemental GID, readiness receipt, and complete
source/content identities are equality-validated rather than embedded as model
literals. The CPU stage uses the distinct reference-data queue, exact 6 CPU / 24
GiB envelope, and matching jackhmmer/nhmmer thread flags; only it mounts the
content-addressed database subtree. The dependent GPU stage mounts parameters,
never the host database, and consumes only the validated preprocessing envelope.
The execution map remains disabled until the stager supplies the final tree
digest; no broad-root placeholder is valid.

The same namespace rule applies to native BindCraft. The scheduling contract
routes every access-profile=`academic` model for the authorized tenant to the
Terraform-provided academic lane; the snapshot freezes the resolved namespace
per stage. Kubernetes create, observe, UID-fenced delete, and result projection
all consume that frozen value. Helm derives academic enforcement from the access
profile and rejects any execution-map binding that differs from the complete
Terraform handoff, so a rendered Job cannot pretend to mount a licensed claim
cross-namespace.

Helm's `scientificBatch.enabled` and `scientificBatch.writesEnabled` gates are
both false by default and must be enabled together. Enabling requires:

- an immutable scheduling-contract ConfigMap and a non-empty generated v3
  execution map, rendered by Helm into an immutable ConfigMap;
- the canonical `scientificArtifacts.enabled` PostgreSQL/S3 service;
- bounded Kubernetes API egress CIDRs;
- Job, JobSet, Workload, and Pod RBAC plus workload NetworkPolicies in every
  unique execution-map namespace;
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
receipts, the dual-target BindCraft MPNN projection and explicit runtime gate,
the frozen v6/v7-to-v8 real-PostgreSQL write paths, delayed artifact publication,
fan-out and gang rendering, routed
LocalQueue namespaces, required GPU pool affinity, and quota handoff (including
DELETE 202, a still-present UID, confirmed absence, and a DELETE 409 fence),
canonical scheduling routing/deadlines, same-poll QuotaReserved persistence,
complete phase ingestion, exact Kueue preemption/retry, stale-attempt
observations, non-retryable taxonomy,
the model/collector exit handshake (positive completion after pending polls,
non-zero model exit, OOM and preemption classification, a missing result, and
both the observer's and the collector's own deadline bounds),
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
