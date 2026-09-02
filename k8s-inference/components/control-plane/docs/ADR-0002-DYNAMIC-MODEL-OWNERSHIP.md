# ADR-0002: Dynamic model ownership and migration

- Status: accepted for incremental implementation
- Date: 2026-09-01
- Decision owner: NIM Fast Start Platform

## Context

Terraform currently renders individual model workloads. That is a safe bootstrap
mechanism, but it makes routine model operations depend on a Terraform plan and
creates an unsafe split-brain risk if a live controller later edits the same
objects. Operators need to change hot replica floors, ceilings, placement inside
existing pools, cache policy, and exposure without changing cloud
infrastructure.

## Decision

`inference.fs2.nebius.ai/v1alpha1` `ModelDeployment` is the versioned,
experimental live model API. Compatibility is not promised until a conversion
and storage-version upgrade policy is accepted. One FS2 controller is the sole
writer of every generated serving object.
Terraform installs the API, controller, and infrastructure envelopes but does
not create a `ModelDeployment` or generated model object after that model has
been adopted.

The API is runtime-neutral. A renderer translates a qualified catalog template
to Kubernetes objects. The initial renderer adopts the existing manifest
bundle; KServe `LLMInferenceService`, KServe `InferenceService`, GAIE/llm-d,
and Dynamo remain renderer choices behind the same API.

Kubernetes status is the live scheduling and readiness truth. PostgreSQL stores
append-only revisions, audit, approvals, and idempotency records. It never
decides where or when a pod runs.

## Single-writer ownership matrix

| Object or field | Sole writer | Readers | Notes |
| --- | --- | --- | --- |
| Cluster, node groups, pool min/max, capacity reservations, VPC, storage classes, registry | Terraform | Controller, admin API | Live requests outside these envelopes return `InfrastructureRequired`. |
| CRDs, controller Deployment/RBAC, upstream operators, Gateway, database, observability | Terraform | Platform workloads | Installing a capability is an infrastructure change. |
| `ModelDeployment.spec` | Authenticated admin API using Kubernetes optimistic concurrency | Controller, audit writer | Human `kubectl` writes are policy-dependent; the admin API remains the supported mutation path. |
| `ModelDeployment.status` and finalizer | FS2 model controller | Admin API, catalogs, metrics | Status is never copied from desired state without observation. |
| Generated Deployment, Service, KEDA, warm/cache Job, route and publication binding | FS2 model controller, field manager `fs2-model-controller` | Kubernetes controllers | Terraform and users do not patch generated objects. |
| Deployment replica count when KEDA is selected | KEDA-generated HPA via `/scale` | FS2 controller | The FS2 controller owns scaler policy, not the live replica value. |
| Queue admission and job quota state | Kueue | FS2 controller, admin API | Kueue handles jobs/admission, not serving Deployment replicas. |
| Gateway route status | Gateway controller | FS2 controller, admin API | FS2 owns desired route; Gateway owns route status. |
| Artifact/cache state | Cache/localization controller or Job, projected into `ModelDeployment.status` by FS2 | Admin API | Cached and Ready are independent states. |
| Model revisions, approvals, idempotency and audit | PostgreSQL transaction owned by the admin backend | Admin API, auditors | Append-only history; not a scheduler or readiness source. |
| OpenAI and MCP publication | FS2 catalog projector | Public APIs | Publication requires observed `Ready=True`, desired exposure, and caller policy. |

## Reconciliation invariants

1. Every generated object has a controller owner reference to exactly one
   `ModelDeployment`, the labels `fs2-serve.nebius.ai/model-deployment` and
   `fs2-serve.nebius.ai/model-id`, and the annotation
   `fs2-serve.nebius.ai/spec-digest`.
2. Server-side apply uses field manager `fs2-model-controller`; force-conflicts
   is false outside an explicitly verified adoption operation.
3. A reconcile reads the object again after every write. Status advances only
   from observed generation, resource UID, readiness, cache, admission, and
   route observations.
4. Renderer output is deterministic for one API object, catalog revision, and
   infrastructure-envelope revision. Its canonical digest is recorded in
   status and audit.
5. Retries are bounded and idempotent. A retry of the same generation and input
   digest cannot create a second serving identity.
6. The supported admin deletion workflow is two-phase: the admin API patches
   `.spec.lifecycle.desiredState=Draining` with a zero hot floor, waits for
   publication withdrawal, bounded active work, and observed replicas to reach
   zero, and only then deletes the CR. The controller never writes `.spec`.
   Its finalizer is a fail-closed backstop for an unexpected delete: it renders
   a zero-scale/no-publication projection and waits for the same observations
   before deleting generated objects. Finalizer removal additionally requires
   authoritative, complete owned-resource discovery to be empty.
7. `Ready=True`, `Cold=True`, `Loading=True`, `Draining=True`,
   `InfrastructureRequired=True`, and `Failed=True` are mutually exclusive
   terminal phase projections. `Cached` is orthogonal and may be true while a
   model is cold or ready.
8. Desired state alone never publishes a model. Disabled, draining, failed,
   infrastructure-required, unauthorized, or not-yet-observed models are absent
   from OpenAI and MCP catalogs.

## Infrastructure boundary

The live API may select compatible pools and queues that already exist inside a
signed infrastructure envelope. It may not create or enlarge a pool, change a
GPU shape or capacity type, attach a capacity block, create storage, install an
operator, add a cluster privilege, or select an unqualified artifact/runtime/GPU
combination. Such a request performs no Kubernetes workload or cloud mutation
and returns a machine-readable Terraform handoff naming the missing envelope.

The customer API names a pool rather than a vendor GPU resource. The envelope
maps that pool to resource names, accelerator class, topology, architecture,
capacity type, and scheduling labels. This keeps the API usable on H100, H200,
B200, B300, GB300, RTX PRO 6000, and heterogeneous clusters when a model/runtime
qualification exists.

## Zero-downtime adoption protocol

Adoption is per model and gated by `.spec.adoption.mode`; observed progress is
reported in `.status.adoption.state`.

1. **Inventory:** record every Terraform address, live UID, generation,
   resource version, canonical object digest, field manager, route, scaler, and
   current image digest. Reject unrecognized writers or drift.
2. **Quiesce:** freeze model configuration changes. Keep the serving objects and
   traffic running. Create the matching `ModelDeployment` with
   `.spec.adoption.mode=Observe`; the controller is observe-only.
3. **Pre-diff:** render the controller candidate and prove that all serving,
   scheduling, security, storage, and routing fields match the live object after
   excluding controller-owned status and computed replica fields.
4. **Release Terraform ownership:** apply a reviewed Terraform change that
   removes only this model's objects from configuration and state without
   destroying them. The apply receipt names the exact UIDs and pre-diff digest.
5. **Claim:** after verifying Terraform state is empty for the inventory, the
   controller performs one server-side apply with the adoption receipt and
   owner references. It does not force an unresolved field conflict. The
   immutable receipt is set while moving `.spec.adoption.mode` from `Observe`
   to `Claim`; a successful observation reports `.status.adoption.state=Owned`.
6. **Post-diff:** re-read every object, verify stable UIDs, no workload rollout,
   Ready endpoints, route acceptance, scaler ownership, and an empty Terraform
   plan for the released addresses. Set `adoption-state=owned` only after this
   proof. `Claim` cannot be downgraded or rebound to another receipt.
7. **Rollback before claim:** delete the observe-only CR and restore the exact
   Terraform configuration/state inventory.
8. **Rollback after claim:** pause the controller for this model, remove owner
   references without deleting objects, import the exact UIDs into the reviewed
   Terraform addresses, prove a no-diff plan, then remove the CR finalizer and
   CR. Terraform and the controller are never writers at the same time.

An adoption receipt is immutable and contains the model namespace/name/UID,
Terraform state serial and lineage, source commit, object identities, canonical
pre/post digests, and approval identity. Secrets and model payloads are never
included.

Raw or foreground Kubernetes deletion can allow garbage collection to race the
drain backstop. The live writer therefore remains feature-gated until a
validating admission policy/webhook rejects deletion unless the observed
Draining preconditions are satisfied. The pure planner rejects incomplete
discovery and never treats an empty, non-authoritative inventory as proof.

## Failure and recovery

- Controller restart: recompute the deterministic plan from the CR generation
  and live inventory; resume without creating new identities.
- Partial apply: repair only the missing or drifted owned object and retain a
  truthful non-Ready phase until all observations converge.
- Preemptible-node loss: Kubernetes/KEDA/node autoscaling recovers within the
  existing envelope; `nodePending` and phase timestamps remain observable.
- Stale admin ETag or duplicate idempotency key: reject or replay the stored
  result before writing the CR.
- Renderer or admission failure: set a bounded condition reason and retain the
  last Ready revision until rollout policy permits replacement.
- Lost PostgreSQL availability: reconciliation of existing CRs continues;
  new audited admin mutations fail closed.

## Consequences

Routine model operations no longer require Terraform, while infrastructure
changes remain reviewable. The migration requires a deliberate one-model-at-a-
time state handoff and conformance tests. Helm installing the CRD does not by
itself transfer ownership: the writer, admin mutations, and adoption path stay
feature-gated until their live acceptance is complete.

## Current feature gate and next tranche

The source-only implementation installs the versioned API definition and
provides authenticated, read-only admin validation/render previews:

- `POST /admin/api/v1/model-deployments:validate-preview`
- `POST /admin/api/v1/model-deployments:plan-preview`

The preview service has no Kubernetes writer dependency. Apply, adopt, and
delete routes return `501 model_deployment_writer_disabled`. A preview is
optimistically bound to the current ETag, records a secret-free audit event,
and states `mutation_supported=false`.

The second source tranche adds an append-only PostgreSQL desired-revision
ledger, HMAC-only idempotency receipts, atomic revision audit records, and
append-only bounded status observations. It also provides optional,
tenant-filtered read seams:

- `GET /admin/api/v1/model-deployments`
- `GET /admin/api/v1/model-deployments/{name}`
- `GET /admin/api/v1/model-deployments/{name}/history`
- `GET /admin/api/v1/model-deployments/{name}/status`

These routes are absent unless a read service is explicitly injected. The
default runtime does not mount them, no mutation service exists, and a database
revision never claims that Kubernetes has applied or observed it. Status is
reported as unavailable or stale until a matching controller observation is
durable.

The chart keeps the CRD under `crds/` for fresh standalone Helm installs. Helm
does not upgrade or delete CRDs. Solution installs therefore apply and upgrade
that same file through the explicit Terraform
`kubernetes_manifest.model_deployment_crd` resource, wait for `Established`,
and order the Helm release after it. Installing the CRD creates no controller
or model workload.

The next tranche must implement and test all of the following before enabling
mutation or adoption:

- an approval/admission workflow that is the only caller of the durable
  revision append seam, preferably through a dedicated database writer role,
  plus lifecycle and retention policy for its receipts;
- explicit rollback target semantics (the current durable action is an audit
  label, not proof of which earlier revision was selected) and conformance
  checks that the selected spec is an exact prior revision;
- a controller-writer identity and fencing contract for status events,
  monotonic Kubernetes resource-version handling, and a transactionally
  consistent status/head read so a stale controller cannot publish newer
  readiness evidence;
- an operational HMAC key-retention policy that keeps every idempotency key
  version for at least the receipt replay horizon and blocks removal while a
  retained receipt still depends on it;
- a Kubernetes server-side-apply adapter, controller Deployment, leader
  election, least-privilege RBAC, finalizer/status writer, bounded work queue,
  metrics, and restart/failure recovery;
- validating admission for raw delete, adoption state transitions, artifact and
  infrastructure-envelope freshness, and cross-tenant policy;
- authoritative owned-resource/managed-field discovery and the reviewed
  Terraform-state adoption receipt workflow;
- real idle-time and warm-window scheduling (the API fields are currently
  validation-only);
- Kueue localization/warm/benchmark jobs, queue status, cache and snapshot
  localization, and qualification observations;
- HTTPRoute generation, a Ready-gated publication projector, and dynamic
  OpenAI/MCP catalog consumption;
- complete controller phase/condition projection and the add/edit/drain/delete,
  rollback mutation APIs and UI; the source-only read/history/status seams must
  be wired only with the reviewed production repository.

Until these gates pass, no retained-cluster model may be adopted and Terraform
continues to own every existing per-model workload.
