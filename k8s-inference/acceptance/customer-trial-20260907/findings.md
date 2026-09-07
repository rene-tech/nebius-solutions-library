# Observed usability findings

These findings were observed during the trial rehearsal and inspected without
changing the production platform. The campaign finished with 13 successful
scientific operations and one scheduler-blocked/cancelled case; one of 71 normal
inference probes also failed. The scheduling failure is a genuine customer blocker;
the separate display defects are not evidence that completed artifacts were lost.

## Blocker: an admitted BindCraft batch cannot fit its assigned pool

The second BindCraft operation was admitted to the `h100-1x` preemptible flavor
at 15:05:04 UTC. Its GPU stage requests 16 CPU plus a 0.1-CPU collector. Kueue
recorded 16,100 millicores, while the one-GPU node has 16,000 physical and 15,900
allocatable millicores, even before other Pods. The scheduler reports
`Insufficient cpu`; the autoscaler reports `NotTriggerScaleUp` because another
node of the same shape cannot make the Pod schedulable. The admitted workload
does not move back to the reserved flavor after capacity there becomes free.

This is not a normal cold-node wait. The first BindCraft run succeeded on the
reserved pool. The second cannot make progress on its assigned pool without
intervention. After retaining exact Job/Pod/events and resource evidence, the
test manager authorized cancellation of this one task-owned operation so the
remaining campaign could finish. It must remain a failed/blocked case, with the
manual cancellation recorded; no hidden retry or policy repair is permitted.

The public cancellation returned 202 at 15:12:46 UTC; the operation became
`cancelled` and every attempt released its resources. The original case remains
`failed_scheduler_blocked`, not a successful model run. At 15:11:30 UTC the
browser had displayed both operation and attempt as `running`, admission as
`admitted`, and `No error` / `No terminal error`; it did not expose the pending
scheduler condition or CPU-fit reason. A customer could not diagnose or recover
from this using the displayed status alone.

Recommended correction before unattended trials: make pool selection consider
the complete Pod's CPU, memory and accelerator requests against usable node
capacity, including sidecars and system reservations. Route BindCraft only to a
fitting flavor; do not assume that any one-GPU pool can host any one-GPU model.
Handle an admitted-but-unschedulable assignment explicitly (a compatible
re-admission path or an actionable customer error), rather than indefinite
generic waiting. No larger capacity ceiling or architecture rewrite is needed
to establish the immediate fix; a suitable existing reserved flavor already ran
this model successfully. Exact live evidence is retained by the observer lane.

Source inspection supports a targeted fix: the GPU branch of
[the scientific scheduler](../../components/control-plane/src/fs2_serve/scientific_batch/scheduling.py)
intersects declared eligible pool IDs and accelerator identities without the
per-node CPU/memory-envelope check performed by its CPU-stage branch.
[Kueue Terraform](../../modules/kueue-scheduling/main.tf) couples aggregate CPU,
memory and accelerator quota by flavor; that aggregate accounting is not a
single-Pod bin-packing guarantee. The scientific-only eligible pool declarations
in [workload queue wiring](../../stages/workloads/queue.tf) must not be mistaken
for a demonstrated fit on every declared node shape.
These inspected scheduling and admin source files are unchanged between the
deployed control-plane source `0c1c6f9e2` and baseline main `b707b5e0c3`.

## Availability defect: one ordinary MCP inference request failed

Qwen MCP probe 46 began at 15:12:02.515582 UTC and failed after 0.96245 seconds,
before receiving an operation ID. No client retry was made. Subsequent independent
requests passed. Server logs at 15:12:03.426 establish an unexpected
`invoke_model` exception because the registry considered the model not routable;
the MCP transport response was HTTP 200. Consequently HTTP status counters alone
must not be interpreted as successful model calls.

The [MCP handler](../../components/control-plane/src/fs2_serve/mcp_server.py)
revalidates routes and asks [the registry](../../components/control-plane/src/fs2_serve/registry.py)
for an enabled model. The registry raises `RuntimeError` for a disabled route,
but that branch of the handler does not translate this exception into its
structured model error. Both inspected files match the deployed source.
The exact reason the route became disabled is unproven; the appearance of a
burst Pod alone does not prove causation. The bounded controller-log window
showed successful reconciliations, but not their complete status payloads. The
existing Grafana reporting role denied a historical status-event SELECT; no
permission changes or alternate-credential workaround were attempted.

Recommended correction before claiming smooth continuous availability: resolve
the transient route loss and preserve service from healthy replicas where
applicable; expose structured retryable/unavailable tool results with correlation
IDs instead of a generic internal exception. Measure MCP tool outcomes in
addition to HTTP status. The test client's exception-only logging also needs
better nested diagnostic capture, but it did not cause the server-side error.

## Scientific catalog gives misleading snapshot availability

The scientific catalog within `/admin/scientific-runs` displays
`Checkpoint unavailable · GPU snapshot unavailable`, while the full model
inventory and dispatch-policy panel offer qualified matching snapshot options.
The Protenix switching request actually restored its configured CUDA snapshot.

The [catalog projection](../../components/control-plane/src/fs2_serve/scientific_admin_catalog.py)
sets the caching checkpoint/snapshot fields to `unavailable` and does not join
the installed scientific startup registry in that projection. The
[catalog table](../../components/admin-console/src/pages/scientific/ScientificRunsPage.tsx)
renders those values directly. A separate run's `not-observed` restore status
is valid when that run used normal loading and must not be conflated with this
catalog inconsistency.

Recommended correction: project the same qualified option identities used by
the full inventory and policy form, while keeping available capability,
selected policy and actual per-run restore evidence distinct.

## Result downloads are not wired into the operator artifact table

Completed RF results pass semantic validation and public API download/hash
checks, but the browser artifact table shows access as `Not available`.
The [PostgreSQL artifact adapter](../../components/control-plane/src/fs2_serve/scientific_admin_postgres.py)
explicitly returns `download.available=False`, no href, and an instruction to
use the authorized artifact endpoint. This is a missing UI retrieval action,
not a demonstrated storage or public-API authorization failure.

Recommended correction: expose a user-initiated authorized download action for
the output manifest and its artifacts, with a clear API alternative while the
action is absent. Do not make the customer inspect internal identifiers to get
a completed result.

## Completed immutable results incorrectly age into stale observations

This is separate from the missing download action. The same artifact adapter
sets snapshot `observed_at` to the immutable result's `record.committed_at`.
The [scientific admin reader](../../components/control-plane/src/fs2_serve/scientific_admin.py)
then compares that commit time against its observation freshness bound, so a
fresh successful read of an older completed result becomes `STALE`.

Recommended correction: distinguish the time the adapter successfully read the
source from the result's completion/commit timestamps. Keep unavailable-source
errors meaningful without suggesting valid historical results expired.

## Overview compares differently scoped operation counters

Before the campaign, the overview compared a small recent durable terminal
count with a much larger lifetime Prometheus value. In
[admin.py](../../components/control-plane/src/fs2_serve/admin.py),
`PrometheusQueryTemplates.for_window` windows rates and latency but uses a raw
counter total for `terminal_operations`; `overview` subtracts the durable
usage-window total. Model/tenant scope must also match before reconciliation.

Recommended correction: only compare aligned time windows, model/tenant scopes
and event definitions, or explicitly report that the values are not comparable.
The displayed discrepancy is not evidence of that many missing customer jobs.

## Active run progress needs a manual browser refresh

The RFdiffusion four-shard detail page remained open from 15:13:32 to 15:17:32
UTC and continued showing its 15:13:31 observation/running state. The public
result had succeeded at 15:16:34. A fresh navigation at 15:17:47 showed semantic
PASS, all four successful inference shards, and two preempted first attempts
followed by successful second attempts. This confirms server-side recovery;
it does not show automatic live progress in the browser.

Recommended correction: refresh active operation/attempt state automatically
and show its observation time. Reconcile the aggregate admission summary with
the current shard states while preserving prior preempted attempts as history.
Do not make users infer completion from a stale page or confuse server-side
retries with additional customer submissions.

## Measurement limitations to retain

Hardware GPU-utilization samples are available, but the public overview does
not provide a time-integrated DCGM GPU-seconds value or reliable TTFT. Application
`active_compute` and scheduler-occupied/idle phase clocks are not measurements
of continuously busy CUDA kernels. Any sampled utilization integral must be
labeled as an estimate with its sampling interval.

The 46 scientific lifecycle subjects reconciled, but the aggregated restore
phase remained zero even though exact Protenix runtime logs prove CUDA/CRIU
restoration. Reconciliation of durations does not establish correct phase
classification. Fix that classification before comparing snapshot costs or
presenting zero restore time as a real measurement. The whole-cluster sampled
idle estimate includes the original resident fleet; it is not this customer's
billable idle-GPU time.

An initial auxiliary `/healthz` probe returned 404 because that was not the
documented readiness path. The interactive HTTP/MCP results remain separate;
the sampler was handed over to `/readyz` without replaying inference requests,
with both harness versions and the handoff gap retained. This is a test-client
mistake, not a demonstrated service outage.
