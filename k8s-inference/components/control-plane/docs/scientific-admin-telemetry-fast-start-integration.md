# Scientific admin, telemetry and fast-start integration

This note records the current-main integration review performed before the
first live scientific run. It is deliberately limited to operator and
attribution behavior: aggregate scientific workload profiles and execution
maps remain owned by the first-live-run lane.

The frozen baseline is `origin/main` at
`76c26e179af1adb4931fa33601248a560cbf94bd`.

## Exact source review

| Candidate | Current-main result |
| --- | --- |
| Scientific admin `820f3b0672025586f0243caaceb421eacbf918d6` | Already an ancestor; no port required. |
| Lifecycle bridge `9e77aeba79ce1c2336e8b1121ac7899af11e7c08` | Already an ancestor; no port required. |
| Priority and fair-capacity foundations `884702991ce51b2a0c2c82e9f9cf899e0d820049` / `7d022551936d05dd3c9ef3241d09d3b00a96773e` | Already ancestors; no port required. |
| Collector failure handshake `7d91604bfb79e0b1df4ee69970b2c27afc59d8e9` | Equivalent behavior is already present as `7f22da36c86d5236d1141f9ff93acad946f61f7c`; no duplicate port. |
| PostgreSQL cancellation and legacy-state recovery `4fb590e6341dbf2af9d379343609de1705659b12` | Equivalent behavior is already present as `341d2cbb89db723305cf707b5b4aff0f1cc7da8c`; no duplicate port. |
| Workload telemetry `6c8ff4d7ae3e6e37b3d4a9c9afafccace6b8cb9c` | Equivalent append-only lifecycle subjects, correlations, signals and rollups are already present as migration `0018_workload_lifecycle_telemetry.sql`; no duplicate port. |
| Fast-start mechanisms `ca57b2bb7b41e058159b676c20cea7ae17a31464` | Missing from current main and integrated by this successor. |

The fast-start integration adds regional-cache, explicit host-memory
residency, and GPU-resident mechanisms. A configured mechanism is operator
detail and never grants a customer-facing level. `Off` remains the truthful
level until the exact model, runtime, artifact, pool and mechanism identity has
a failure-free measured cohort that satisfies the fast-start evidence policy.

## Operator surfaces

The authenticated admin API exposes scientific capability, run, run-detail and
model-readiness projections at:

```text
GET /admin/api/v1/scientific-capabilities
GET /admin/api/v1/scientific-runs
GET /admin/api/v1/scientific-runs/{run_id}
GET /admin/api/v1/scientific-models
GET /admin/api/v1/telemetry/workloads
GET /admin/api/v1/telemetry/workloads/{subject_id}
```

HTTP and MCP share the same scientific service and authorization checks. MCP
provides model discovery, submission, status, cancellation, event, artifact,
result, upload and byte-download tools. The admin console reports unavailable
readers and missing observations explicitly instead of turning them into empty
or zero-valued state.

Dynamic model deployment options are compiled from the installed
infrastructure envelope. Tenant, queue, priority, pool and cold-start mechanism
choices are therefore server-authoritative, while the fast-start target remains
operator-editable. Selecting a mechanism narrows placement to its declared
pools and applies its required cache tier, hot floor and replica headroom.
Mechanism declaration internals and snapshot identities stay locked to the
installed model tuple. This prevents a form from presenting arbitrary cache or
snapshot configuration as qualified.

## Accelerator metadata layering

Portable catalog metadata is not a statement about the accelerator installed
in a particular cluster. Admin model list and detail views derive `gpu_class`
from the selected installed pool. The regression test intentionally keeps the
portable Qwen catalog entry on its B300 source value while configuring an H100
pool, then proves both admin views report `nvidia-h100-sxm5-80gb` without
rewriting the catalog. This corrects the H100 operator view without hard-coding
H100 into reusable source contracts.

## Lifecycle attribution

The append-only lifecycle ledger separates quota-reserved,
scheduler-occupied, and device-allocated clocks. Scientific bridge tests cover
queue and admission, image pull, artifact load, restore, warmup, active
compute, allocated idle, cooldown/grace, preemption and teardown. GPU seconds
are partitioned once by deterministic precedence, and an absent PodResources
or DCGM identity remains an explicit reconciliation gap rather than a
synthetic device UUID.

## Deployment boundary

This integration branch is not a deployment branch. It performs no shared
cluster, Terraform, registry or GPU mutation. The first-live-run owner must
integrate this successor with the currently deployed shared-service lineage
before any rollout.

## Residual gaps

- No mechanism declaration is invented for a model. Models without an
  installed Terraform declaration offer only the conventional path.
- The scientific admin projection remains read-only; run cancellation is
  available through the authorized public operation and MCP surfaces, not an
  admin mutation control.
- A configured mechanism still reports fast-start level `Off` until exact
  measured evidence qualifies a level. The retained comparison does not grant
  a production tier.
- Device-clock reconciliation remains unavailable until a trusted
  PodResources/DCGM producer supplies exact Pod UID, rank and GPU UUID
  correlation. Scheduler occupancy remains visible and the missing device
  evidence is reported as a gap.
- Aggregate scientific profiles, execution maps and live route activation are
  intentionally absent from this successor and remain with the first-live-run
  owner.
