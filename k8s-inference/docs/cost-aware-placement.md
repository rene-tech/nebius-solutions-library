# GPU preference and available-capacity fallback

Owner requirement, 2026-10-08: each App prefers the qualified execution shape
with the lowest **cost per successful request**. If it cannot serve the request,
use the next available qualified shape. A GPU-hour price alone is not the
objective. Do not strand requests on an unavailable favourite.

## Current implementation

The shared-serving controller checks cold activations against Ready nodes,
cordons, taints/tolerations and whole-Pod CPU, memory, accelerator, ephemeral
storage and Pod-slot headroom. Allocation includes Pods in every namespace and
the existing canonical init-container/sidecar/overhead arithmetic. Kubernetes
still decides placement; this observation is not a reservation. KEDA remains
the only autoscaled replica owner. The total replica ceiling is unchanged.

The next activation returns to the cheapest measured available qualified pool.
When cost evidence is missing, use the existing qualified fallback order and
report it as **unmeasured**, not cost-optimized. When observations are incomplete,
retain the observed layout, with the older positively-confirmed lost-node
fallback still available. Loading, serving and draining Pods are not migrated.
Idle fleet reconciliation does not create fallback HPAs or wake idle Apps.

## Durable cost evidence

Reuse `fs2_benchmark_campaigns`, `fs2_benchmark_trials` and their existing lease,
idempotency and immutable-result APIs. No JSON file/LLM placement authority and
no new scheduler or database migration are introduced.

A placement-enabled campaign explicitly sets `placement_target`:

- exact public model ID and immutable runtime image;
- accelerator count, representative workload class, cold/warm/snapshot state;
- one case per qualified pool, identical input digest, at least three repetitions;
- each case's `compute_rate`: USD per whole replica-hour, dated source and
  allocation basis (all GPUs and CPU/RAM share, or the whole allocated node).

Each trial records `allocated_compute_seconds`: actual elapsed allocation of
that priced replica, summed across attempts. Include attributed staging,
initialization, restore, computation, checkpoint/export and idle/cooldown.
Apportion shared allocation once; do not charge a whole shared Pod independently
to every concurrent request. Waiting without an allocation is queue latency,
not GPU cost. Do not substitute response time for this measurement.

`cost/success = sum(all attempts' allocated seconds × replica USD/hour) /
(3600 × semantically successful requests)`.

The newest complete matched campaign supplies the preference. Incomplete
campaigns, missing allocation measurements, semantic failures without cost
accounting, wrong runtime images and wrong GPU counts do not replace previous
evidence. Rates are immutable snapshots, not a live pricing feed: publish a new
campaign/rate revision after prices or runtime/workload assumptions change.

Operators can inspect the source and missing coverage through:

`GET /admin/api/v1/performance/placement/{model_id}?runtime_image=...&accelerators_per_replica=1&cache_condition=cold`

This uses the existing operator session and returns measured order, source
campaign/trial IDs, rates and cost estimates, or `state: unmeasured`.

## Deliberate remaining work (not a fleet-wide completion claim)

- The current 106 successful historical registry trials have no occupied-GPU
  measurements, and mostly cover one GPU type per model. None was backfilled
  with invented allocation cost. Collect real comparable cost cohorts for all
  qualified model/pool pairs, including representative request-size classes.
- Shared-serving cost selection currently applies at **cold activation**.
  A busy App retains its layout: qualification of overflow scale-out into a
  second pool without shrinking occupied sibling segments is still required.
- Scientific batch Jobs retain their existing Kueue path and September
  admitted-unschedulable recovery. The new cost preference is not yet integrated
  with their per-attempt admission. Simply sorting an `In` affinity list does
  not make Kueue choose the cheapest ResourceFlavor. Finish and qualify that
  integration before claiming all scientific models use cost-ranked dispatch.
- Availability is bounded by already-qualified runtime/GPU/memory/cache pairs.
  This policy does not declare an untested model compatible with another GPU.

These gaps are tracked in Task Deck
`fs2-cost-ranked-gpu-placement-r20261008`. Running customer operations, tenant
limits, credentials and scientific inputs are not changed by this release.
