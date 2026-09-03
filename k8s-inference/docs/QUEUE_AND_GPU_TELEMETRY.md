# Shared GPU scheduling and lifecycle telemetry

This document defines the provider-neutral queue and correlation foundation for
interactive inference, demonstrations, and scientific batch workloads sharing
one Kubernetes cluster. It intentionally does not assign an unverified GPU
total to any customer. Physical capacity comes from `accelerator_pools`; queue
floors and relative weights are operator policy in `deployment.scheduling`.

## Scheduling contract

The Terraform renderer maps four independent concepts onto Kueue 0.17:

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

For every pool:

```text
physical capacity = sum(ClusterQueue nominal floors) + Cohort shared quota
```

Terraform rejects negative/fractional GPU counts, unknown pools, duplicate or
incomplete pool orders, lending above a nominal floor, and total floors above
the maximum autoscaled physical capacity. A zero nominal quota remains in each
ClusterQueue manifest because Kueue requires that `(flavor, resource)` entry
before the queue can borrow the Cohort's shared quota.

ClusterQueue weights control fair borrowing between tenants. LocalQueue
weights control usage-decayed admission between lanes within one ClusterQueue.
A weight is relative, not a fixed percentage. Kueue samples LocalQueue usage
every five minutes with a seven-day half-life. This favors an under-served lane
without turning a temporary idle floor into stranded capacity.

The public request selects only `service_class`. The scheduler/control plane
must persist its resolved immutable snapshot rather than accepting caller
supplied queue, priority, flavor, or GPU-resource fields:

| Service class | Default WorkloadPriorityClass | Value | Victim expectation |
| --- | --- | ---: | --- |
| `platform-critical` | `platform-critical` | 10000 | not a victim |
| `presentation` | `presentation` | 1000 | restartable |
| `interactive` | `interactive` | 100 | restartable |
| `customer-batch` | `standard` | 0 | restartable |
| `bulk-backfill` | `batch` | -100 | checkpointable |

These are Kueue workload priorities, intentionally independent of Kubernetes
Pod `PriorityClass`. A queue can reclaim borrowed capacity with
`reclaim_within_cohort = "LowerPriority"`; a lane that cannot checkpoint should
not be described as checkpointable merely to make preemption easier.

An operator can add primary and secondary scientific lanes by supplying
LocalQueues with different weights and model/tenant selector sets. The
scientific controller resolves the unique most-specific route before operation
admission and freezes its namespace and LocalQueue in the scheduling snapshot;
it rejects ambiguous selectors and any execution namespace mismatch. Native
BindCraft/PyRosetta and AlphaFold3 therefore route to the namespace-local
`fs2-academic-poc/academic-scientific` queue rather than claiming that the
licensed PVC can be mounted by a Job in `fs2-models`. They additionally require
the academic access profile and immutable asset/access receipt before Job
creation.

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
provide time series; PostgreSQL remains the durable source for lifecycle events
and reconciled rollups. The database ledger, Job label propagation, derived
rules, dashboards, and live contention/retry tests are separate implementation
steps. The Terraform in this change creates the queue and telemetry substrate
but does not claim those application-level pieces are already complete.

## Verification

Run these before a deployment:

```bash
terraform -chdir=modules/kueue-scheduling test
python3 -m unittest tests.test_scheduling_observability_contract
terraform -chdir=stages/foundation validate
terraform -chdir=stages/workloads validate
```

The pinned Kueue, OpenTelemetry, Tempo, and DCGM values must also render with
their Helm charts. A shared-cluster deployment needs a reviewed Terraform plan
and an explicit rollout window because changing quota policy can affect pending
workloads. No live cluster change is implied by this document.
