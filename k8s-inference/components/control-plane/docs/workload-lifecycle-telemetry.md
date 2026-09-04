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

- register an attempt subject after its immutable scheduling snapshot exists;
- append Kueue admission, PodScheduled/release and lifecycle phase edges using
  deterministic attempt/event keys;
- append exact Workload/Job/Pod/node/GPU correlations as UIDs become known;
- append preemption/retry events without reusing the prior attempt identity;
- reconcile after terminal artifact and semantic-validation state is durable.

These calls are additive and do not give the telemetry lane ownership of
controller, artifact or scientific-admin state.

The terminal artifact bridge additionally passes the typed canonical
`ScientificRunResult` to `ScientificResultLifecycleProjector`. That projection
is the authority for effective `service_class`, frozen ClusterQueue/LocalQueue,
and the per-attempt Kueue pool, ResourceFlavor, accelerator resource/count and
admission timestamp. It does not accept a look-alike scheduling DTO. These
facts are stored on an immutable admission signal and mirrored onto bounded
OpenTelemetry attributes. Result projection never turns `started_at` /
`completed_at` into quota, scheduler, device or phase duration: missing
controller/DCGM clocks remain explicit reconciliation gaps.

The canonical result exposes Pod, node and GPU identity sets. A single-Pod
attempt can therefore retain an exact Pod/GPU/rank correlation. For a
multi-Pod attempt the projector keeps every Pod and node identity but does not
pair GPU UUIDs by tuple order; PodResources/DCGM enrichment must supply that
join.

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
uv run pytest -q tests/test_lifecycle.py tests/test_lifecycle_api.py
uv run pytest -q -m postgres tests/test_postgres_integration.py
uv run ruff check src tests
uv run mypy src/fs2_serve

cd ../../observability
./scripts/test.sh
```

Migration `0018_workload_lifecycle_telemetry.sql` follows the integrated
artifact migration `0014`, scientific-controller migration `0015`, and its v7
and v8 state upgrades (`0016`/`0017`). Deployment authorization is `0019`.
Upgrade tests freeze both legacy controller state versions before `0018`, then
apply the complete ordered chain and prove prior migration digests and durable
rows are unchanged. The PostgreSQL release contract is generated once from
that combined chain; no isolated migration list is accepted.
