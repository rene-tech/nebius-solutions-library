# Workload lifecycle telemetry and GPU accounting

The lifecycle ledger is the durable attribution source for online operations
and scientific-batch attempts. OpenTelemetry preserves causality; Kubernetes,
Kueue, kubelet and DCGM provide independently observed allocation facts. The
ledger never derives GPU allocation from request latency.

## Immutable identity

`fs2_telemetry_subjects` binds one subject to tenant, opaque principal and API
key identities, request/operation, batch/workload/attempt, exact model revision,
protocol and W3C trace identity. `fs2_telemetry_correlations` adds immutable
Kueue Workload, Job/JobSet, Pod, node, GPU UUID and rank joins. Event and
correlation keys are globally idempotent: replaying identical facts succeeds;
reusing a key for different facts fails closed.

Online request subjects use the operation UUID for subject, request and
workload identity. Scientific execution creates one subject per immutable
attempt UUID and retains its parent operation and batch/workload IDs. The
scientific controller owns those registrations; artifact storage remains the
artifact service's responsibility and is referenced only by immutable,
credential-free URI and digest.

## Three clocks and phase partition

The ledger records three clocks separately:

1. `quota_reserved`: Kueue admission until quota release, multiplied by the
   admitted accelerator count.
2. `scheduler_occupied`: each scheduled Pod GPU rank until Pod/replacement
   release, including image pull and all occupied idle time.
3. `device_allocated`: the exact Pod UID, GPU UUID and rank mapping observed by
   kubelet/PodResources or DCGM until that mapping disappears.

Every scheduler-occupied rank is partitioned exactly once. Where independently
observed phases overlap, deterministic precedence is: active compute,
checkpoint/drain, restore, compile, artifact load, image pull, warmup, workflow
wait, resident idle, cooldown/grace, teardown, then unclassified. Duplicate
windows for the same Pod UID/rank are merged before accounting.

For a completed subject:

```text
scheduler_occupied_gpu_seconds
  = sum(phase_gpu_seconds) + reconciliation_delta_seconds

occupied_idle_gpu_seconds
  = scheduler_occupied_gpu_seconds - active_compute_gpu_seconds
```

The allowed absolute partition delta is the largest of 1 ms, 1% of the
accounting clock, or two source-resolution buckets per occupied rank. Two
buckets cover independently sampled start and end edges. A missing scheduler
or device clock, invalid/incomplete interval, or device time exceeding Pod
occupancy beyond that tolerance prevents a rollup from being called
reconciled. Unclassified time is preserved rather than guessed and is surfaced
as a data gap.

Shared online runtimes are handled deliberately: a request subject records its
active-compute span and Pod/GPU correlation, but does not claim the Pod's whole
resident allocation. The shared runtime residency must be a separate workload
subject. This prevents overlapping concurrent requests from each claiming the
same device-allocated interval.

## Application and controller instrumentation

Inbound W3C trace context is persisted as bounded IDs and continued by the
background `fs2.operation` consumer span. Admission emits receive/enqueue facts;
worker attempts emit admit/retry facts and a child `fs2.runtime.invoke` span.
When a trusted runtime metadata provider returns Pod/node/GPU identity, the
control plane records the correlation and active-compute interval. It still
waits for Kubernetes/DCGM evidence before a rollup can reconcile.

The scientific controller integrates through `LifecycleRepository`:

- register one attempt subject after its immutable scheduling snapshot exists;
- replay the durable scientific-batch event stream into queue, Kueue admission,
  quota-reserved, cleanup, preemption and terminal edges;
- observe PodScheduled plus init and regular-container timestamps for image
  loading, artifact loading, restore, warmup, active compute and allocated idle;
- append exact Workload/Job/Pod/node/GPU correlations as UIDs become known; and
- close still-open quota, Pod, device and phase intervals only after the exact
  attempt resource is confirmed released.

This is a projection into the existing `PostgresLifecycleRepository`; it does
not create a scientific display ledger. The scientific event table is the
restart/replay source. Globally stable event and correlation keys make an
identical replay a no-op, while reuse with different facts fails closed. The
controller persists its own transition before projecting it, then replays all
durable transitions when it next claims the run. A crash on either side is
therefore repaired without charging an interval twice.

Pod phase attribution is init-container aware. Kubernetes may report a Pod as
`Running` while `prepare-workspace`, runtime artifact verification, restore or
warmup init containers are still executing. The bridge does not emit active
compute until every declared init container has terminated successfully and
the `scientific-stage` container has an actual running/terminated timestamp.
The collector-only window after model exit is allocated idle. Waiting and
container-creation windows remain image loading or allocated idle rather than
being guessed as compute.

Node identity comes from the Node object's immutable UID. Exact device identity
is accepted only from the trusted `telemetry.fs2.nebius.ai/gpu-uuids` Pod
annotation, whose ordered JSON array must equal the stage's admitted GPU count.
Scientific workload Pods have `automountServiceAccountToken: false`, so a model
container cannot write this evidence. If no trusted PodResources/DCGM enricher
has supplied the annotation, scheduler occupancy and phase accounting remain
available but the device clock is intentionally absent and reconciliation
reports that gap. No synthetic UUID is generated.

At terminal release every inapplicable or unobserved required phase receives a
zero-length `unavailable` interval. This makes the full accounting vocabulary
queryable without inventing elapsed time. Restart, repeated observation,
preemption and cancellation tests assert stable ledger cardinality and stable
GPU-second rollups.

These calls are additive and do not give the telemetry lane ownership of
controller, artifact or scientific-admin state.

## Security, retention and cardinality

Raw prompts, sequences, images, request/response values, credentials, bearer
headers, cookies and exception strings are never ledger fields or span
attributes. Caller-controlled JSON field names are SHA-256 hashed. Input/output
shape contains only counts and scalar type/size information. Parameter values
are represented by one canonical SHA-256. Artifact URIs reject user info,
queries and fragments so presigned credentials cannot be persisted.

Lifecycle signal `detail` accepts only bounded operational codes and immutable
digests. The database tables reject update/delete through triggers. The runtime
role receives select/insert only; Grafana's reporting role sees aggregate views
without principal, API-key, trace, payload-shape or artifact-URI fields.

Raw traces and Loki logs stay for seven days. Prometheus keeps 15 days, bounded
by 45 GB, and holds only tenant/model/phase/quality and reconciliation series;
request, attempt, Pod, node and GPU UUID never become lifecycle metric labels.
The exporter refuses more than 65,536 lifecycle series. PostgreSQL facts and
immutable rollups are the durable aggregate export and are retained
indefinitely by this slice. Shortening that retention requires a reviewed
additive migration plus an externally retained export receipt; no background
delete or mutation path exists.

## Operator surfaces and data gaps

Authenticated endpoints provide tenant-authorized list and detail views:

```text
GET /admin/api/v1/telemetry/workloads
GET /admin/api/v1/telemetry/workloads/{subject_id}
```

The list exposes latest immutable rollups. Detail exposes exact correlations
and signals and always declares `payloads_exposed: false`. Prometheus exports
phase GPU-seconds, all three clocks, terminal workload counts, absolute
reconciliation delta and unclassified time. The lifecycle dashboard filters by
tenant/model and links operators to the existing Tempo/Loki exploration plane.

Expected gaps are explicit: an application-only online request lacks scheduler
and device clocks; a Kubernetes-only attempt can lack DCGM mapping; DCGM cannot
identify image-pull time before a container/device mapping exists; and a lost
event edge leaves an incomplete interval. None is silently estimated.

## Verification

```bash
cd components/control-plane
uv run pytest -q tests/test_lifecycle.py tests/test_lifecycle_api.py \
  tests/test_scientific_lifecycle_bridge.py
uv run pytest -q -m postgres tests/test_postgres_integration.py
uv run ruff check src tests
uv run mypy src/fs2_serve

cd ../../observability
./scripts/test.sh
```

Migration `0018_workload_lifecycle_telemetry.sql` follows the integrated
artifact and scientific-controller migrations. Before a shared control-plane
rollout, regenerate the PostgreSQL release contract from the combined branch.
