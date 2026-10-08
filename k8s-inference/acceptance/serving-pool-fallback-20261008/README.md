# Shared-serving GPU-pool recovery — 2026-10-08

## Incident and scope

WhiteLab/Artemis DiffDock operation `34bb6446-7fd9-4507-abb4-883fbb33f4f6`
was accepted 08:39:11 UTC and expired 10:39:11 without a ready or running runtime.
Four preferred h100-1x nodes had Ready=Unknown since 08:18; two qualified
h100-reserved-8x nodes were Ready with spare GPUs. The serving renderer gave
the entire four-replica burst allowance to the first pool and created no usable
fallback segment. September's admitted-unschedulable recovery covers scientific
batch Jobs, not these shared-serving Deployments.

The recorded failure is `lease_recovery_exhausted`, not a successful inference
because status polls returned HTTP 200. No claim is made about the cloud cause
of the lost nodes beyond the observed Kubernetes conditions.

## Release

- Source: `aa8d04c7910c34d9ec259a0863d254500c5c8757`, branch
  `agent/fs2-reliability-recovery-20261007` in rene-tech/nebius-solutions-library.
- Immutable API/controller/maintenance image:
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:597732bbdc89a1aa5d9e7a5d04295090064e14d85891823fd94db87d154dc7e4`.
- Scientific collector/tools remain independently qualified digest `22ee25fc…`.
  Model images, scientific execution maps, customer keys/limits, model desired
  specs, Gateway, Services and customer workbenches were not changed.
- Kubernetes context: `nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`.
- API 3/3 and controller 2/2 updated/Ready. Public readiness remains healthy.
- Chart RBAC adds only list access to nodes and Pods for placement observations.
  The controller still owns generated workloads; KEDA still owns replicas.
- Private H100 root tfvars now pins this image. Only the digest line changed;
  `terraform fmt -check` and root `terraform validate` pass. No broad Terraform
  apply or fresh-cluster acceptance was performed.

Cold activations use a qualified pool with whole-replica headroom rather than
waiting on an unavailable/full/absent favourite. Running and initializing Pods
are preserved. Cost preferences use the existing immutable benchmark registry;
see [cost-aware placement](../../docs/cost-aware-placement.md) for the exact
contract and the still-open fleet-wide work.

## Acceptance (unchanged final runtime release)

Existing system/qa identity, reusable DiffDock-only key, concurrency two. No
customer key was used. Each cohort sends independent REST and typed MCP jobs,
replays idempotency keys, polls durable status, retrieves results and downloads
the externalized native artifacts. The MCP path also checks `get_operation_result`
and the MCP download handle against the REST result. Object downloads do not
receive the platform API key.

Native validator checks success, exact full 1UBQ receptor (78,570 bytes), aspirin
identity, one 13-atom/13-bond finite-coordinate pose, finite confidence and
trajectory shape; downloaded bytes must match the artifact size and SHA-256.

| Cohort/path | Operation ID | Acceptance → ready | Acceptance → completed | Client through validated download |
| --- | --- | ---: | ---: | ---: |
| B REST, cold App/cached node | `52080dbc-06aa-46ce-94e2-fd3b78bd34ef` | 33.42 s | 39.61 s | 44.46 s |
| B MCP, same activation | `bb4b350f-2846-491d-badd-835934ea2364` | 27.81 s | 34.46 s | 44.58 s |
| C REST, warm | `61846544-6e27-417e-9a6f-d6dcda4a4945` | 0.96 s | 4.36 s | 13.07 s |
| C MCP, warm | `73700dd4-874e-4b89-85ec-34c8c98a03b6` | 0.77 s | 4.92 s | 19.12 s |

All four succeeded with native validation and idempotency checks. Client time
includes the deliberately 10-second polling cadence and artifact download;
it is not GPU execution time. Runtime receipts show all four calls used the
first Ready replica (Pod UID `9b240f02-e0c9-46b1-9bf1-e6eff7960c3d`, one GPU,
regular capacity, first attempt); this is concurrent public-request coverage,
not proof that both GPUs executed.
The second requested replica was still pulling on the other qualified node.
At 14:26 UTC KEDA reduced DiffDock to zero and both Pods disappeared without
manual scaling. All GPU serving Deployments were then at zero; the CPU MSA
service remained at one. The normal 300-second cooldown begins after the last
active scaling signal (14:20:56), including the bounded startup-retention
history, not necessarily exactly at the last HTTP completion.

Before the final release, candidate A also completed both operations, but
required ~269 seconds to readiness on a fresh image cache. It is not counted
as a final-release cohort. Its receipt was re-read after completion, so its
short replay/download stopwatch is **not** the original cold-start duration.
Candidate r1 also exposed idle HPA bootstrap activation; r2/r3 require real
demand and remove that unwanted fleet warm-up. Retain these findings rather
than reporting only the best timing.

Focused final regression suite: 85 passed (two dated, pre-existing fast-start
fixture failures excluded after reproducing them on the baseline). A separate
22-test registry suite, including five real PostgreSQL contracts, passed on a
task-owned local database. These test groups overlap. Ruff and diff checks pass.
The disposable local database was stopped/removed after testing.

## Customer preservation and remaining work

Lynx `07f87c97-5a40-4bfe-b6c3-3edd76fa270c` retains Pod UID
`f0b7e1fa-4182-4c86-9604-bffa81455e07`, zero container restarts and advancing
durable checkpoint generations (268 before this work, 286 during verification).
No customer operation was cancelled or restarted.

The original WhiteLab operation remains expired: no unsupported DB reset,
customer-credential QA call or silent resubmission was performed. A new request
can use the repaired serving path; this release is not a replay of its private
scientific input or a claim to qualify every DiffDock workload.

The live cost API truthfully returns `unmeasured`. Historical benchmark data
does not establish allocation-inclusive comparative cost for every model. New
measurements, scientific Kueue cost dispatch and busy-App overflow remain in
`fs2-cost-ranked-gpu-placement-r20261008`; do not mark that work complete based
on these four calls.

Private build/rollback manifests, exact tfvars backup, API receipts, native
outputs and observations are retained under
`/home/tux/secure-handoff/fs2-serving-pool-fallback-20261008/`.
Use `release-r3/*.rollback.json` only after confirming no intervening release;
those test-and-replace patches restore r2 without altering customer workloads.
