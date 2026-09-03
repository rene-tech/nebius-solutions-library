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
| `cpu_classes_schema` | The class contract version those entries conform to, carried in the same bytes. |
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

### One schema, one assembler, one document

Several Terraform owners contribute CPU stage classes.
`modules/kueue-scheduling` is the sole assembler: `stages/workloads` merges
every contributor's entries and the module emits the merged map as
`cpu_classes` inside the one content-addressed scheduling ConfigMap, under the
one key `kueue-scheduling.json`. There is no second document and no second key.

The class shape is `catalog/runtime/schema/cpu-stage-classes.schema.json`
(`fs2-serve.nebius.ai/cpu-stage-classes/v1`), named in the emitted bytes as
`cpu_classes_schema`.

| Class | Stage it serves | Contributor |
| --- | --- | --- |
| `reference-data` | AlphaFold 3's raw data pipeline, on the tainted pool that mounts the shared reference databases | this branch, `stages/workloads/queue.tf` |
| `general-cpu` | BindCraft's CPU stages, and collector or aggregation work, on untainted general capacity | `modules/general-cpu-scheduling`, through its canonical `cpu_classes` contribution |

Three rules the assembler enforces, because no single contributor can:

1. **One execution namespace per class.** A consumer keys LocalQueues by bare
   name, so the same name in two namespaces is unrepresentable. A class names
   one namespace, and an owner that runs the same lane in several namespaces
   contributes one class per namespace.
2. **One owner per LocalQueue and per cpu/memory ResourceFlavor.** A name
   claimed twice cannot be resolved back to one placement.
3. **Expected pools are not the actual pool.** `eligible_pool_ids` is the set a
   stage may land on. `pool_resolution.mode` says how the one it landed on
   becomes knowable: `per-pool-flavor` when the class has a single pool whose
   ResourceFlavor names it, so the admission answers it; otherwise
   `node-label-observation`, naming the Node label to read after scheduling,
   because one flavor spanning several pools cannot report which ran the stage.
   A consumer records `expected_eligible_pool_ids` and `actual_pool_id`
   separately and never presents the first as the second.

`cpu_class_digests` carries a SHA-256 per class entry, over that entry alone.
The ConfigMap digest changes whenever any part of the policy changes, so a
consumer that froze one class uses the per-class digest to tell whether that
class changed, and a contributor uses it to confirm the assembler published its
entry unaltered.

### Field names a contributor must match exactly

The canonical names, so a contributor's rename fails a test here rather than a
merge later:

| Canonical | Not | Why |
| --- | --- | --- |
| `pool_resolution.pool_id` | a class-level `pool_id` | A class-level pool ID beside a flavor spanning several pools reads as an assignment nobody made. Pool identity lives only inside `pool_resolution`, and only when the mode is `per-pool-flavor`. |
| `cpu_classes_schema` | `cpu_class_schema` | The version of the class contract the entries conform to, carried in the emitted bytes. |
| `cpu_class_digests` | `cpu_class_entry_sha256` | A SHA-256 per class entry, keyed by class ID. |

Bounds a consumer must match: a pool ID is a lowercase Kubernetes label value
of at most 63 characters, so dots and underscores are legal and a DNS-label
rule would reject real IDs. A node label key and a toleration key are
Kubernetes qualified names, at most 253 characters before the slash and 63
after, 317 in all. A consumer still bounding either at 253 rejects a class
this contract accepts.

A contributor supplies its own class entry and, when its ClusterQueue or
LocalQueue is created elsewhere, the external queue facts alongside it, so the
assembler can check namespace admission and single ownership without a second
definition of the same queue. It never supplies `reference-data`: that entry
belongs to `stages/workloads`, derived from the reference-data plane's own
storage contract, and the assembler refuses a contributed copy rather than
letting merge order decide which definition wins.

### Recording the actual pool

`pool_resolution.mode` decides when the actual pool is knowable, and the
acceptance evidence schema follows it exactly. A CPU receipt records the mode
it froze in `pool_resolution_mode`, so the rule that applies is a recorded
fact rather than an inference:

- `per-pool-flavor`: the class has one pool and its ResourceFlavor names it,
  so the pool is known at `QuotaReserved`, exactly like a GPU attempt.
  `actual_pool_id` is required as a string from then on. Both classes produced
  today are this mode.
- `node-label-observation`: one ResourceFlavor spans several pools, so a quota
  reservation can exist, and can be lost again, with no Pod ever scheduled.
  `actual_pool_id` is null until `pod_scheduled_at` is set, and required from
  then on. This is the only case where null is correct.

A GPU receipt resolves its pool from `resource_flavor_pool_ids` and carries no
`pool_resolution_mode`. The ClusterQueue, the ResourceFlavor, and the reserved
PodSet assignments are known at `QuotaReserved` and stay required throughout. A
collector must never fill in a pool it has not read.

### Preconditions for transplanting a contributed producer

Before a contributed CPU pool owner is merged into this assembler:

- Its default execution namespace must be a real namespace with an owner, not
  a value that collapses to an empty string when an unrelated feature is off.
  A configuration that enables CPU pools with academic assets disabled must
  plan, and a root plan test must cover exactly that combination.
- Explicit licensed-stage namespace routing stays as it is: a licensed claim
  is mountable only from its own namespace, so that class keeps its own
  namespace rather than inheriting a shared default.
- Its ClusterQueue and LocalQueue facts come with it when another owner creates
  them, so the assembler can check namespace admission and single ownership
  without a second definition of the same queue.

**The general-CPU producer is integrated.** `modules/general-cpu-scheduling`
derives its class from the same pool, queue, selector, toleration, and measured
capacity facts that create the general CPU lane. The workloads stage merges
that canonical contribution with the reference-data class, verifies its
per-entry digest, and emits both through the one scheduling ConfigMap whenever
both lanes are enabled. The root facade derives the corresponding fit facts
from `deployment.cpu_pools`; there is no second operator-authored class or
capacity map that can drift.

Absence still means refusal, not substitution. If the general CPU lane is not
configured, `general-cpu` is absent and a BindCraft CPU stage cannot borrow the
tainted reference-data class. A consumer must read the content-addressed
document, verify its raw bytes, and fail any stage whose named class is absent.

### Mapping an observed admission back to a class

A CPU admission reports a cpu/memory ResourceFlavor, and
`resource_flavor_pool_ids` covers accelerator flavors only, so it cannot
resolve one. Each class therefore publishes `resource_flavor`, and that value
is the only key that maps an observation back to a class:

1. Read `status.admission.podSetAssignments[*].flavors` for the `cpu` and
   `memory` resources.
2. Find the single `cpu_classes` entry whose `resource_flavor` equals it.
3. If none matches, or more than one does, refuse the capture and record the
   observed flavor verbatim. Do not fall back to the frozen class, and do not
   guess from the ClusterQueue: an operator can move a class between queues,
   and two classes can share a queue.

A mismatch between the frozen class and the observed flavor is a real event
worth surfacing, in the same way a differing `actual_cluster_queue` is. It
means the stage ran somewhere other than where it was planned.

## Handing over the ConfigMap digest

`scheduling_contract_ref` is the whole handoff: `config_map_name`,
`namespace` (`fs2-system`), `key` (`kueue-scheduling.json`), `schema`, and
`sha256`. The ConfigMap is `immutable: true` and its name carries the first 12
characters of the digest, so a policy change produces a new object rather than
mutating one a controller is already reading.

`sha256` is over the **exact applied bytes** of `data["kueue-scheduling.json"]`.
Terraform's `jsonencode` escapes `<`, `>`, and `&` as `\u003c`, `\u003e`, and
`\u0026` and preserves UTF-8, so parsing the document and re-serializing it
produces different bytes and a different digest. Hash the raw string exactly as
read from the API.

The consuming owner must verify before it writes anything:

1. Read the ConfigMap named by `config_map_name` in `namespace`.
2. Hash `data[key]` as raw bytes and compare against `sha256`.
3. On a mismatch, refuse to create the Job or JobSet and surface the mismatch.
   A mismatch means the policy the controller resolved against is not the
   policy Terraform published, so any admission created from it would record a
   snapshot that never existed.
4. Freeze the verified digest into the attempt's snapshot, so a historical
   attempt can be re-read against the exact policy that produced it.

How the value reaches the controller is the chart owner's decision: mount the
ConfigMap by name, or pass `config_map_name` and `sha256` as environment
values. This branch edits no chart and no controller source, so neither the
mount nor the verification exists yet. **That wiring is the dependency this
branch hands over; it is not implemented here.**

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
  `budgeted` only when `deployment.scheduling.budget_core_resources` is set,
  which removes `cpu` and `memory` from Kueue's exclusions. When it reads
  `excluded-not-budgeted`, Kueue drops core requests before admission and no
  cpu/memory quota in the cluster is enforced.

  Core admission is **pool-coupled**, and this changes what a consumer reads.
  There is no separate core ResourceFlavor: `cpu` and `memory` join the
  accelerator resourceGroup, so the flavor Kueue assigns for a Workload's
  accelerators is the same flavor that granted its cpu and memory. A
  Workload's core reservation is therefore always on the pool that runs it,
  and `core_resource_flavor` is `null` on purpose. `core_capacity` is keyed by
  pool, each entry being that pool's measured per-node schedulable capacity
  times its maximum node count, and `core_queue_quotas` is keyed by queue and
  then by pool. This release supports core admission for exactly one
  accelerator resource name per deployment, because a resource belongs to
  exactly one resourceGroup: pools may differ in GPU class, node size and
  capacity, but they must advertise the same resource key. A mixed-resource
  deployment runs with core admission off. Per-resource ClusterQueue grouping
  is not implemented here.
- Any live apply. No queue, ClusterQueue, namespace, or workload was created or
  changed on a shared cluster by this branch.
