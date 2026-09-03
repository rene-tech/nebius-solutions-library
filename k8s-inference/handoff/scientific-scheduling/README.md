# Scheduling contract handoff to the scientific controller owner

This directory is documentation. It changes no build, no image, and no
Kubernetes object. It exists because the scheduling policy is Terraform-owned
while everything that consumes it — the resolver, the durable snapshot, the
Job/JobSet writer, and the Kueue admission reader — is owned by the scientific
controller branch. Nothing here has been applied to
`components/control-plane/`, deliberately.

Everything below was checked against a real Kueue v0.17.8 / JobSet v0.12.0
cluster or against the controller sources as they stood at commit `a2a14d90`.
Where a claim comes from a live observation, the observation is stated.

## What the contract publishes

The workloads stage renders one immutable, content-addressed ConfigMap in
`fs2-system` and publishes its identity as the `scheduling_contract_ref`
output: `config_map_name`, `namespace`, `key`, `schema`, and `sha256`.

`sha256` is the digest of the **exact applied bytes**. Terraform's `jsonencode`
escapes `<`, `>`, and `&` as `\u003c`, `\u003e`, and `\u0026` and preserves
UTF-8, so re-serializing the parsed document produces different bytes and a
different digest. Read the raw ConfigMap value and hash that.

Fields a consumer needs, beyond the rendered Kueue manifests:

| Field | Meaning |
| --- | --- |
| `service_classes` | Per class: WorkloadPriorityClass and value, default LocalQueue, pool preference, queue/execution ceilings, preemption mode, and `caller_selectable`. |
| `local_queue_routes` | Per LocalQueue: namespace, ClusterQueue, `model_ids`, `tenant_ids`, `service_classes`. |
| `cluster_queue_namespaces` | Every namespace each ClusterQueue admits, including externally owned queues. |
| `cluster_queue_pool_order` | The exact pool search order of each ClusterQueue. |
| `pools` | Pool ID to ResourceFlavor and accelerator resource name. |
| `resource_flavor_pool_ids` | Reverse map: admitted ResourceFlavor to pool ID. |
| `pool_node_label_key` | The canonical pool identity label. |
| `namespace_bound_models` | Models whose assets exist in exactly one namespace. |
| `cpu_classes` | Named placement classes for CPU-only stages. |
| `priority_precedence` | The ordering Kueue actually applies, per ClusterQueue. |
| `core_resource_admission` | Whether cpu/memory are budgeted at all. |
| `external_local_queue_names`, `external_cluster_queue_names` | Objects another Terraform owner creates. |

## Required resolver semantics

Route resolution has three ranks and no lexical tie-break:

1. an exact `tenant_ids` + `model_ids` + `service_classes` route,
2. a wildcard-tenant `model_ids` + `service_classes` route,
3. the service class's `default_local_queue`.

Two matches at the same rank are a configuration error, not a choice. An exact
route deliberately overlapping a wildcard route is the supported way to give
one tenant its own lane, so Terraform enforces uniqueness within each rank
separately and permits the overlap between ranks.

Three refusals are mandatory:

- `caller_selectable: false` (only `platform-critical`) must never be reachable
  from a public request.
- A model listed in `namespace_bound_models` must resolve into that namespace.
  If the resolved lane is in another namespace, refuse. Falling back would run
  the stage where its licensed PersistentVolumeClaim cannot be mounted, and the
  failure would surface much later as a mount error.
- A `default_local_queue` fallback must still satisfy that queue's own route
  constraints. A tenant-restricted lane is not a valid fallback for a different
  tenant.

The resolved LocalQueue's namespace is the workload namespace. Keep the
existing `workload_namespace` and `route_namespace` fields; do not collapse
them.

## CPU-only stages

A CPU PodSet gets no accelerator ResourceFlavor from Kueue, so it inherits no
node labels and no tolerations. On a tainted pool it would never schedule.
`cpu_classes` therefore carries, per class, the LocalQueue, ClusterQueue,
namespace, pool ID, node selector, tolerations, and the pool's **per-node**
schedulable capacity.

The class is frozen per stage. An AlphaFold 3 data pipeline uses the
`reference-data` class, whose lane is a route-less LocalQueue in the licensed
namespace bound to the reference-data CPU ClusterQueue. A tenant, model, and
service class cannot say whether a stage is CPU or GPU, so that lane carries no
route bindings and is selected only through the class.

`schedulable_capacity` is per node, not a pool aggregate: one Pod must fit one
node. Refuse a stage whose request exceeds it instead of creating a Job that
can never be scheduled.

## Pool identity label: a live defect

`POOL_LABEL = "fs2.nebius.ai/pool-id"` in
`components/control-plane/src/fs2_serve/scientific_batch/kubernetes.py` is read
from the admitted ResourceFlavor's `metadata.labels`. Terraform has never
rendered that key. `stages/workloads/queue.tf` labels every ResourceFlavor with
`accelerator.fs2.nebius/pool-id`, which is also the pool node label.

Effect: every GPU admission capture raises "Kueue admitted ResourceFlavor has
no canonical pool identity". The fix is to read the canonical key, which the
contract now publishes as `pool_node_label_key`, and to keep accepting the old
key for an object that already carries it. `resource_flavor_pool_ids` is a
direct alternative that needs no ResourceFlavor read at all.

## Observed Kueue Workload shape for a JobSet

From `modules/jobset-controller/scripts/kind-jobset-kueue-integration.sh`, on
Kubernetes 1.34.0 with Kueue v0.17.8 and JobSet v0.12.0:

- Kueue creates **one** PodSet per `replicatedJob` name, not one per replica.
- Its `count` is `replicas x parallelism`.
- The replicated-job grouping appears as `topologyRequest.subGroupCount` with
  `subGroupIndexLabel: jobset.sigs.k8s.io/job-index`.
- `status.admission.podSetAssignments` has one entry per PodSet, whose
  `resourceUsage` is the whole gang's usage.
- Kueue owns the Workload through an `ownerReference` to the JobSet, admits it,
  and unsuspends the JobSet.

A reader that expects one PodSet per replica will misparse every gang.

## Required admission capture

`status.admission` can be cleared when quota is released, so a poll that misses
the window loses the tuple. Persist it on first observation and never refresh it.

Capture these separately rather than collapsing them:

- `quota_reserved_at` from the `QuotaReserved` condition's
  `lastTransitionTime`.
- `admitted_at` from the `Admitted` condition's `lastTransitionTime`.
- `actual_cluster_queue` from `status.admission.clusterQueue`, which can differ
  from the frozen request and must be compared against it, not assumed.
- The admitted ResourceFlavor, accelerator resource, and count, compared
  against the frozen snapshot.

A QuotaReserved-only state must be representable honestly, and a lost
reservation must be recorded as lost rather than left as a stale admission.

## Required DTO and codec changes

Additive, inside the controller's current state schema. Every new field is
optional on decode so an existing row still loads, and is written only when it
carries a value so unchanged workloads keep byte-identical documents:

- `StageSchedulingDecision.resolved_resource_flavors`: the ordered pool to
  ResourceFlavor mapping, one-to-one with `resolved_pool_preference`. A durable
  consumer can then interpret a historical attempt without today's mutable
  policy. Include it in the snapshot digest only when non-empty, so a record
  written before the mapping existed keeps its recorded digest.
- `SchedulingAdmission.quota_reserved_at` and
  `SchedulingAdmission.actual_cluster_queue`, per the section above.
- Identity bounds matching what the contract now guarantees: ClusterQueue,
  LocalQueue, WorkloadPriorityClass, ResourceFlavor, namespace, tenant, and
  model identities are all at most 63 characters; an accelerator resource name
  is an optional prefix of at most 253 characters plus a name of at most 63;
  priorities and second ceilings are signed int32; at most 64 stages; a stage
  ID starts with a letter.

`catalog/runtime/schema/scientific-run-result.schema.json` is deliberately
untouched by the scheduling branch: it describes what the controller produces,
so it must change together with its producer. The additive shape it needs is
`resolved_resource_flavors` as an optional stage field and `quota_reserved_at`
plus `actual_cluster_queue` as optional `scheduling_admission` fields, with
`policy_revision`, `tenant_queue`, `model_lane`, `checkpoint_mode`, and
`preemption_mode` left as they are so existing v1 results keep validating.

## Verification of these semantics

A resolver implementing exactly the semantics above was written against the
controller sources at commit `a2a14d90` and passed 20 tests there, covering the
three-rank precedence, the academic namespace binding, wrong-tenant refusal,
the DTO bounds, the separate admission fields, and a real Kueue admission
payload. That code is deliberately not carried in this repository: it would be
unexecuted duplicate source that drifts from the controller tree. This document
is the normative contract; the owner implements it against their current tree.

## Not in this branch

- The scientific execution map. The control-plane chart is its single owner and
  its schema version is the controller's to set.
- Nothing about core admission is left unimplemented, but read
  `core_resource_admission` before trusting a cpu/memory quota. It is
  `budgeted` only when `deployment.scheduling.core_capacity` is set, which
  removes `cpu` and `memory` from Kueue's exclusions and renders one
  label-less core ResourceFlavor in its own resourceGroup, shared across the
  accelerator groups, with per-ClusterQueue floors and the residual in the
  Cohort. When it reads `excluded-not-budgeted`, Kueue drops core requests
  before admission and no cpu/memory quota in the cluster is enforced.
- Any live apply. No queue, ClusterQueue, namespace, or workload was created or
  changed on a shared cluster by this branch.
