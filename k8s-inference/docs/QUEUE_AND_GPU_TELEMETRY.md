# Shared GPU scheduling and lifecycle telemetry

This document defines the provider-neutral queue and correlation foundation for
interactive inference, demonstrations, and scientific batch workloads sharing
one Kubernetes cluster. It intentionally does not assign an unverified GPU
total to any customer. Physical capacity comes from `accelerator_pools`; queue
floors and relative weights are operator policy in `deployment.scheduling`.

## Scheduling contract

The Terraform renderer maps four independent concepts onto Kueue 0.17.8:

| Layer | Purpose | Configuration |
| --- | --- | --- |
| `ResourceFlavor` | Select one physical accelerator pool | Derived from `accelerator_pools.<pool_id>` |
| `Cohort` | Hold physical capacity not reserved as a queue floor | `scheduling.cohort` |
| `ClusterQueue` | Give a tenant/workload family a GPU floor and borrowing policy | `scheduling.cluster_queues` |
| `LocalQueue` | Order model or tenant lanes within a ClusterQueue | `scheduling.local_queues` |

The stable `inference-accelerators` ClusterQueue and
`fs2-models/inference-models` LocalQueue always remain. With no explicit
ClusterQueues, the stable queue retains all nominal capacity. When another
ClusterQueue is added without restating the stable queue, the stable queue
becomes a zero-floor member that can borrow from the Cohort. This prevents a
new lane from double-booking the physical quota while preserving existing
queue identity.

Measured on Kueue 0.17.8, not assumed: for a `ResourceFlavor` with no
`topologyName` set, `nodeLabels`, `tolerations`, and a first `topologyName` all
update in place, so those objects carry no replacement trigger and a changed
pool label does not disturb admitted work. This is the only flavor state
Terraform renders here. It is not a claim that `topologyName` is always
mutable: Kueue guards it with a CEL rule that applies once `oldSelf.topologyName`
is set, so changing an existing topology is refused. A topology-aware flavor
would need a replacement path this module does not yet have, and the Kind proof
measures both states so the difference stays visible.
`LocalQueue.spec.clusterQueue`, by contrast, is immutable. The stable
`fs2-models/inference-models` identity is permanently bound to the stable
`inference-accelerators` ClusterQueue. Every additional LocalQueue has an
explicit Terraform replacement trigger for namespace or ClusterQueue changes.
That replacement briefly removes the queue, so operators must drain it first;
an in-place binding update is never an upgrade path.

For every pool:

```text
physical capacity = sum(ClusterQueue nominal floors) + Cohort shared quota
```

Terraform rejects negative/fractional GPU counts, unknown pools, duplicate or
incomplete pool orders, lending above a nominal floor, and total floors above
the maximum autoscaled physical capacity. A zero nominal quota remains in each
ClusterQueue manifest because Kueue requires that `(flavor, resource)` entry
before the queue can borrow the Cohort's shared quota.

`admission_checks` is an advanced pass-through and defaults to empty. This
repository does not create an `AdmissionCheck` or install its controller. An
operator may reference one only after that exact AdmissionCheck and controller
are deployed and Ready; otherwise Kueue deliberately keeps the ClusterQueue
inactive or its workloads waiting. Nebius node-group autoscaling does not
require this field. The checked deployable path and server fixture keep it
empty; a separate server-side dry-run covers only the 0.17.8 CRD shape.

The v2 accelerator-pool handoff reports physical GPUs per node but not the
advertised extended-resource units per node for MIG slices. Workload planning
therefore fails closed for active MIG modes; generic MIG quota is blocked on
the separate MIG contract adding an exact `resource_units_per_node` fact.

ClusterQueue weights control fair borrowing between tenants. LocalQueue
weights control usage-decayed admission between lanes within one ClusterQueue.
A weight is relative, not a fixed percentage. Kueue samples LocalQueue usage
every five minutes with a seven-day half-life. This favors an under-served lane
without turning a temporary idle floor into stranded capacity.
Kueue 0.17.8 rejects nonzero weights at or below `1e-9`; the root facade,
staged handoff, and module all require a value strictly greater than `1e-9` so
that an accepted Terraform plan cannot fail only at the admission webhook.

The public request selects only `service_class`. The scheduler/control plane
must persist its resolved immutable snapshot rather than accepting caller
supplied queue, priority, flavor, or GPU-resource fields:

| Service class | Default WorkloadPriorityClass | Value | Victim expectation |
| --- | --- | ---: | --- |
| `platform-critical` | `platform-critical` | 10000 | not caller-selectable; restartable |
| `presentation` | `presentation` | 1000 | restartable |
| `interactive` | `interactive` | 100 | restartable |
| `customer-batch` | `standard` | 0 | restartable |
| `bulk-backfill` | `batch` | -100 | restartable |

These values are strictly ordered by Terraform; ties and inversions are
rejected. They are Kueue workload priorities, intentionally independent of
Kubernetes Pod `PriorityClass`.

### What priority does and does not guarantee

With `admissionScope.admissionMode = UsageBasedAdmissionFairSharing`, Kueue
0.17.8 orders pending Workloads inside a ClusterQueue by each LocalQueue's
decayed fair-share usage **before** it compares WorkloadPriorityClass. So:

- Inside **one** LocalQueue, priority then creation time decides admission.
- Across **different** LocalQueues on the same ClusterQueue, the less recently
  served lane can be admitted first even when the other lane holds a higher
  service class. A presentation lane does not categorically precede a bulk lane
  in a different LocalQueue.
- Across ClusterQueues, reclaim is additionally bounded by Fair Sharing
  strategies and queue share, so numerical priority is not an eviction
  guarantee either.

### Core admission is coupled to the pool

`scheduling.budget_core_resources` removes `cpu` and `memory` from Kueue's
exclusions, which is what makes any cpu/memory quota enforceable at all, and it
budgets them **on each accelerator pool's own ResourceFlavor** rather than on a
shared one. Kueue assigns exactly one ResourceFlavor per resourceGroup per
PodSet, so putting the accelerator resource, `cpu` and `memory` in one group
means a Workload's cores come from the pool that granted its accelerators. A
separate core flavor cannot promise that: a Workload could hold accelerators in
one pool against cpu and memory measured on another and then fit no node.

Each pool's budget is derived, never typed: its measured per-node schedulable
capacity times its maximum node count. The measurement is declared per pool
with its origin — the pool it was read from, the command, the capture time and
the SHA-256 of that payload — because a bare pair of integers cannot be
checked, and a preset's nominal size is not schedulable capacity. A queue's
core floor is the same share of a pool that its accelerator floor is, and the
remainder of that pool stays in the Cohort.

Shipped plan examples cannot contain a target cluster's measurement. They mark
their synthetic record as `fixture:utf8:<pool-id>` and hash exactly that suffix;
replace the capacity and evidence together before deployment. A command or URI
source, by contrast, is a measurement claim whose referenced bytes must match
the recorded digest.

The cost is a real limit of this release, stated rather than worked around:
**core admission supports exactly one accelerator resource name per
deployment.** A resource belongs to exactly one resourceGroup, so a second
accelerator resource cannot ride with `cpu` and `memory` at all. Pools may
still differ in GPU class, node size, capacity type and node counts; what they
must share is the resource key Kueue budgets, such as `nvidia.com/gpu`. A
deployment that mixes a full-GPU resource with a MIG-slice resource must leave
`budget_core_resources` off and run without cpu/memory quota. Per-resource
ClusterQueue grouping is not implemented in this release, and Terraform refuses
the combination rather than pretending otherwise.

### Cross-queue displacement is not a consequence of priority

Kueue's `reclaimWithinCohort` preempts only what another ClusterQueue borrowed
above its nominal quota. Work inside that queue's own floor is not reclaimable
at any priority. A bulk ClusterQueue holding a floor beside a presentation or
interactive lane in another queue is therefore refused outright: either the
classes share one ClusterQueue, where `withinClusterQueue` preemption applies,
or the lower-priority queue holds a zero floor so everything it runs is
borrowed and can be taken back. There is no acknowledgement that accepts the
starvation. Both CPU ClusterQueues set `withinClusterQueue: LowerPriority` for
the same reason: they sit outside the Cohort, so in-queue displacement is the
only mechanism they have.

### Fair-share usage is not normalized across resources

Kueue 0.17 computes a LocalQueue's fair-share usage by summing each resource's
`resource.Quantity` magnitude multiplied by that resource's weight, and every
weight defaults to 1. There is no unit normalization. While cpu and memory are
excluded from budgeting this does not matter, because only accelerator counts
are summed. Once `scheduling.core_capacity` turns core admission on, a
Workload's memory contributes its size **in bytes**: 80 GiB is roughly
8.6e10, against a GPU count in the single digits. Memory then determines the
ordering almost entirely, and cpu outweighs GPUs as well.

Read plainly: with core admission on and no weights set, lane ordering tracks
memory demand, not accelerator demand. Nothing here claims that two lanes with
similar GPU demand are ordered fairly against each other, because they are not.

The only control Kueue offers is an explicit weight per resource, and
`scheduling.fair_share_resource_weights` in the root tfvars sets
`admissionFairSharing.resourceWeights` on the controller.

The policy is empty or complete, never partial. Kueue defaults an unspecified
resource to weight 1, so naming only the GPU would leave memory at its raw
byte magnitude and the ordering unchanged while appearing corrected. Terraform
therefore requires the map to name exactly the resources this deployment
budgets: every accelerator resource its pools advertise, plus cpu and memory
whenever core admission is on. Weights are nonnegative, zero means the resource
is ignored in the ordering, and at least one must be positive. The effective
map and the exact set of budgeted resource names are published in
`effective_configuration.scheduling` so a reviewer can check them before
anything is created.

Choosing the numbers needs the deployment's own hardware: to make one GPU
comparable to one node's worth of memory, the memory weight is about the
reciprocal of the per-GPU memory in bytes, and the cpu weight about the
reciprocal of the vCPUs per GPU. The default is empty, which keeps upstream
behaviour exactly as it is rather than substituting a guess.

Terraform therefore refuses to render a ClusterQueue that serves more than one
lane under usage-based admission fair sharing unless the operator sets
`fair_share_precedence_acknowledged = true`. The three honest options are:

1. Route the classes that must outrank each other through **one** LocalQueue,
   where priority is decisive.
2. Set `admission_fair_sharing = false` on that ClusterQueue, which restores
   priority-then-timestamp ordering and gives up usage-based fairness.
3. Acknowledge fair-share ordering and give the protected lane an explicit
   nominal floor large enough for its own work.

The rendered contract publishes the ordering actually configured, per queue, in
`priority_precedence`, so an operator explanation cannot claim a guarantee the
scheduler does not provide.

Terraform publishes the complete non-secret policy in an immutable,
content-addressed `fs2-system` ConfigMap. The workloads output
`scheduling_contract_ref` is the only supported handoff: it records the name,
key, schema, and SHA-256. Changing the policy changes the ConfigMap name so a
consumer configured by name receives a deliberate rollout instead of observing
partly changed admission policy. The same document is the input for scientific
API admission and the read-only admin policy view.

Only `restartable` is executable in this scheduling policy. Terraform rejects
`non_preemptible` because the current Cohort policy cannot enforce that promise,
and rejects `checkpointable` because no checkpoint handshake or durable resume
artifact contract exists yet. Those modes remain catalog capabilities for a
future implementation, not selectable service-policy values or live claims.

### Licensed academic lanes

A PersistentVolumeClaim can only be mounted from its own namespace, so licensed
academic work (AlphaFold 3 parameters, the BindCraft PyRosetta prerequisite)
runs in the claim namespace rather than the default model namespace. Three
things follow, and Terraform enforces all of them:

- The ClusterQueue's `namespaceSelector` uses `matchExpressions` with an
  explicit `In` list, because it must admit both the model namespace and the
  claim namespace. `required_namespaces` injects the claim namespace even when
  an operator restates the stable ClusterQueue entry.
- Exactly one Terraform owner renders each queue, and they are not all the same
  owner. `modules/academic-assets` already owns the licensed GPU LocalQueue
  beside the claim and namespace it serves, so the scheduling module describes
  that queue in its contract and never creates it, which is what
  `external_local_queue_names` records. The reference-data plane owns its CPU
  ClusterQueue and ResourceFlavor. `modules/kueue-scheduling` owns everything
  else, including the licensed CPU lane, and those additive queues depend on
  both modules so a fresh apply cannot race a namespace or a ClusterQueue.
- The lane is an **exact** tenant+model route derived from
  `academic_assets.tenant_id`, `academic_assets.namespace`, and the declared
  asset model IDs, and it carries every service class. `namespace_bound_models`
  records the binding so a consumer must refuse a request that would resolve
  such a model into any other namespace instead of silently losing the claim.

An operator can add primary and secondary scientific lanes by supplying
LocalQueues with different weights and model/tenant sets. The contract exports
these bindings separately from Kubernetes manifests. A scientific resolver
must choose one exact tenant+model+service-class route, then one
wildcard-tenant model+service-class route, then the service-class default.
Multiple matches at either rank are a configuration error, never a lexical
tie-break. Terraform rejects duplicate bindings. Native
BindCraft/PyRosetta and AlphaFold3 Jobs additionally require the academic
access profile and immutable asset/access receipt before Job creation.

Pool search order comes from the pools' own facts, not from their names. The
default order puts capacity that is always there before capacity the provider
can reclaim, and a pool with a node floor before one that must scale up, each
group alphabetical so the result is stable. Alphabetical pool IDs alone would
put `h100-preemptible` ahead of `h100-warm` and send work to reclaimable nodes
while a warm node idles. `scheduling.default_queue_pool_order` sets that order on the stable
ClusterQueue and `cluster_queues.<name>.flavor_order` sets it on any other.
An explicit order can choose between equally stable pools, but Terraform rejects
one that moves preemptible or scale-from-zero capacity ahead of a warmer tier.
That is the whole operator decision: a service class with no
`pool_preference` inherits the order of the ClusterQueue it routes to. Setting
`pool_preference` explicitly is still allowed, and it must then name the same
order as that queue, because a class advertising an order the queue does not
search would report a placement that never happens. Terraform refuses the plan
when the two disagree. `examples/scheduling-academic-raw-af3.tfvars` orders
warm capacity first with one setting.

Every pool in one model's eligible set must advertise the same extended
resource name. A Workload requests exactly one resource and Kueue never falls
back across a resourceGroup, so a set naming both a full-GPU pool and a
MIG-slice pool has one reachable entry and one that silently never admits. The
root facade refuses that set before the infrastructure stage creates anything,
and the workloads stage refuses it again.

The pure scheduling resolver intersects the contract's `model_eligible_pool_ids`
with `service_classes.<class>.pool_preference`, refuses an empty intersection,
and constrains the Pod to the remaining set with required node affinity on
`accelerator.fs2.nebius/pool-id`, because Kueue chooses a flavor from the
queue's own order and reads no annotation. Eligibility comes from the
authoritative model placement contract, or from an explicit declaration for a
scientific-only model, never from a pool name. The resolver then uses
`pools.<id>.accelerator_resource_name` instead of scanning every extended
resource in a heterogeneous ClusterQueue. For controller consumption it freezes
the queue, ClusterQueue, WorkloadPriorityClass/value, ordered compatible pools,
accelerator resource/count, maximum queue/execution seconds, checkpoint mode,
preemption mode, contract revision, and capture time. The frozen snapshot
contains the pool-to-ResourceFlavor mapping, so a durable consumer can
interpret historical attempts without the current mutable policy. PostgreSQL
persistence and Kueue observation are controller-owned integration, not part
of this scheduling successor. That integration must separately retain the
Workload UID and full quota-reservation tuple at `QuotaReserved=True`, and must
timestamp admission only from `Admitted=True.lastTransitionTime`.

### CPU-only stages

A CPU-only stage (an MSA or data pipeline) receives no accelerator
ResourceFlavor, so it inherits neither node labels nor tolerations. The
reference-data CPU pool is tainted so unrelated work stays off it, which would
leave such a stage unschedulable. The contract therefore publishes named `cpu_classes`: per class the LocalQueue,
ClusterQueue, namespace, pool ID, exact node selector, exact toleration, and
the pool's advertised per-node schedulable capacity. A consumer resolves the
class a stage belongs to and fails closed when this deployment provisions
none. A consumer must apply the
selector and toleration to a CPU PodSet and must refuse a stage whose request
exceeds the advertised capacity rather than creating an unschedulable Job.

### Observed Kueue Workload shape for a JobSet

Verified on a pinned Kind cluster with Kueue v0.17.8 and JobSet v0.12.0:
Kueue creates **one** PodSet per `replicatedJob` name, whose `count` is
`replicas x parallelism`, and records the replicated-job grouping in
`topologyRequest.subGroupCount` with
`subGroupIndexLabel: jobset.sigs.k8s.io/job-index`. There is not one PodSet per
replica. A consumer reading admission must use this shape; see
`modules/jobset-controller/scripts/kind-jobset-kueue-integration.sh`.

An admitted ResourceFlavor maps back to its accelerator pool through the
canonical label `accelerator.fs2.nebius/pool-id`, published as
`pool_node_label_key` and rendered onto both ResourceFlavor metadata and pool
node labels. `resource_flavor_pool_ids` is the direct reverse map. No other key
is a valid pool identity.

The controller-owned Job/JobSet writer must project the resolved LocalQueue and
WorkloadPriorityClass through Kueue's standard labels, use the
`kueue.x-k8s.io/max-exec-time-seconds` label for execution ceilings, enforce
queue expiry from each attempt's durable `queued_at`, and inject required pool
affinity. The scheduling successor does not contain that lifecycle code. The
task-owned acceptance IndexedJob alone uses
`kueue.x-k8s.io/job-min-parallelism`; ordinary independent Jobs do not claim
partial admission.

The scheduling policy is canonical JSON in an immutable, content-addressed
`fs2-system` ConfigMap, and `scheduling_contract_ref` publishes its name, key,
and SHA-256. That SHA-256 is the digest of the exact applied bytes: Terraform's
`jsonencode` escapes `<`, `>`, and `&` and preserves UTF-8, so a consumer must
hash the raw ConfigMap value rather than a reserialization of the parsed
document. The scientific execution map is a separate contract with a single
owner, the control-plane chart; this scheduling path neither defines nor
publishes it.

## Supported Kubernetes minors

Kueue v0.17.8's upstream end-to-end matrix covers Kubernetes 1.33-1.35.
JobSet v0.12.0's published table covers 1.32-1.34. FS2 keeps those upstream
claims separate and additionally qualifies the exact pinned JobSet/Kueue pair
on Kind 1.35.0 and the managed H100 Kubernetes 1.35.6 cluster. Scientific batch
therefore supports the 1.33-1.35 intersection; 1.35 is an FS2 qualification,
not an upstream JobSet-matrix claim. Managed patch upgrades inside those
minors are accepted. Exact sources, pins, tests, live evidence, and rollback
are recorded in modules/jobset-controller/QUALIFICATION.md.

## True-gang prerequisite

Enabling scientific batch also enables a Terraform-owned JobSet foundation.
It consumes the official JobSet chart directly by OCI digest (the provider
infers version `0.12.0`) and verifies chart digest
`sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24`,
and renders the controller image at
`registry.k8s.io/jobset/jobset:v0.12.0@sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d`.
The chart's CRD is explicitly server-side applied from the same digest before
Helm runs with `skip_crds=true`, because Helm does not upgrade `crds/` content.
The JobSet and Kueue controllers are pinned to
`workload.fs2.nebius/system=true`. Kueue itself uses chart digest
`sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354`
and controller image digest
`sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6`.
That exact Kueue chart renders CRDs from `templates/crd/`, so they remain
Helm-owned and upgrade with the release; a second server-side-apply owner is
forbidden. The pinned server fixture performs a Helm upgrade, proves an
existing LocalQueue UID survives, and confirms v1beta2 remains served and
stored.
Kueue installation waits for JobSet. Before foundation state can become the
workloads input, the readiness gate requires a Kubernetes minor inside the
supported set above, an Established `jobsets.jobset.x-k8s.io` CRD serving
`v1alpha2`, an available controller, discovery, and a successful server-side
dry-run JobSet. The root facade checks the Kueue matrix before infrastructure
planning and applies the narrower intersection whenever scientific batch is
enabled.
The pinned Kind server test installs Kueue with the exact production
`stages/foundation/values/kueue.yaml`, verifies the live v1beta2
`Configuration` contains AdmissionFairSharing, creation-age requeue,
wait-for-Pods-ready, integrations, and core-resource exclusions, and proves
the controller becomes Available when the optional JobSet CRD is absent.
Listing the JobSet integration is therefore safe in the non-scientific
foundation path; enabling true gang work still requires the separately pinned
JobSet CRD/controller readiness contract above.

## Correlation contract

The same identity must be carried through the durable operation, Kueue
Workload, Job/JobSet, Pod labels, OpenTelemetry resources, and lifecycle
ledger. The Kubernetes-to-OTel projection implemented here is:

| Durable/API field | Pod label | OTel resource attribute |
| --- | --- | --- |
| `model_id` | `fs2.nebius.ai/model-id` | `fs2.model.id` |
| `workload_id` | `fs2.nebius.ai/workload-id` | `fs2.workload.id` |
| `attempt_id` | `fs2.nebius.ai/attempt-id` | `fs2.attempt.id` |
| `tenant_id` | `fs2.nebius.ai/tenant-id` | `fs2.tenant.id` |
| `service_class` | `fs2.nebius.ai/service-class` | `fs2.service.class` |
| `resolved_local_queue` | `fs2.nebius.ai/local-queue` | `fs2.kueue.local_queue` |

Opaque `user.id`, `tenant.id`, and `api.key.id` attributes are retained for
correlation. API-key values, authorization headers, cookies, and raw customer
payloads are not telemetry attributes.

OpenTelemetry is the event/correlation plane. The gateway now writes raw traces
to a seven-day persistent Tempo backend while retaining span-derived Prometheus
metrics and Loki logs. One deployment-mode collector watches Kubernetes Events;
node DaemonSets continue to collect container logs and enrich them with the
bounded Pod labels above. A singleton watcher avoids duplicate lifecycle
events.

DCGM is the device-activity plane. The standard exporter collection and
Prometheus scrape cadence is five seconds; the explicit cold-start campaign
retains one-second GPU-utilization/framebuffer sampling. `honorLabels: true`
keeps the workload namespace, Pod, and container emitted by dcgm-exporter as
canonical labels. Pod UID and GPU UUID are retained so a device sample can be
joined to an exact attempt; request and principal IDs stay out of high-frequency
Prometheus series.

## Lifecycle accounting

The application/controller implementation should emit timestamps for:

```text
received -> validated -> enqueued -> quota_reserved
-> node_requested -> node_ready -> pod_scheduled
-> image_pull -> artifact_load -> restore -> compile -> warmup -> runtime_ready
-> gpu_allocated -> active_compute/workflow_wait (repeatable)
-> cooldown_or_grace -> checkpoint_or_drain -> gpu_released -> terminal
```

Keep three clocks separate: Kueue quota-reserved time, scheduler Pod-occupied
time, and device-allocated time. Partition each device allocation into loading,
restore, compile/warmup, active compute, workflow wait, resident idle,
cooldown/grace, checkpoint/drain, and `unclassified`. Never infer a missing
phase from request latency and never double-count overlapping ranks on the same
GPU.

Tempo and Loki provide bounded raw debugging evidence; DCGM/Kueue/Prometheus
provide time series; PostgreSQL must remain the durable source for lifecycle
events and per-attempt reservation/admission facts. Polling alone has a known
evidence limit: Kueue can clear `status.admission` when quota is released, so a
controller outage spanning reservation and eviction can miss the tuple unless
a prior poll persisted it. This scheduling task does not claim event/watch-
complete accounting. Live contention, preemptible-loss,
scale-to-zero, and GPU accounting acceptance remain separate shared-cluster
steps; this implementation does not claim those tests have run.

## Verification

Run these before a deployment:

These need `terraform`, `kubectl`, `helm`, `crane`, `kind`, `jq`, and `python3`
on PATH. The two server scripts create and delete their own local Kind cluster
and touch no cloud project or shared cluster.

```bash
terraform -chdir=modules/kueue-scheduling test
terraform -chdir=modules/jobset-controller test
terraform -chdir=modules/academic-assets test
modules/kueue-scheduling/scripts/server-dry-run-v0.17.8.sh
modules/jobset-controller/scripts/kind-jobset-kueue-integration.sh
pytest -q tests
terraform -chdir=stages/foundation validate
terraform -chdir=stages/workloads validate
```

The pinned Kueue, OpenTelemetry, Tempo, and DCGM values must also render with
their Helm charts. A shared-cluster deployment needs a reviewed Terraform plan
and an explicit rollout window because changing quota policy can affect pending
workloads. No live cluster change is implied by this document.

The later live procedure and deterministic task-owned Job renderer are in
`acceptance/scientific-scheduling/`. They deliberately create no queue policy
and require an applied, revision-checked contract before any submission.
