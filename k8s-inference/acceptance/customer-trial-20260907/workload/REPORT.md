# Scientific customer workload result — 2026-09-07

The bounded campaign completed, but it was **not a clean pass**: 13 of 14 public operations succeeded; one repeat BindCraft operation was admitted to a node flavor that could never satisfy its CPU request. We explicitly cancelled that test-owned operation after preserving scheduler evidence. It was not retried or counted successful.

All nine requested model/profile IDs succeeded at least once. The RFdiffusion → Protenix → Mosaic switching segment passed. A real four-shard RFdiffusion batch completed while higher-priority requests preempted and automatically restarted two bulk shards. No scientific fixture, startup policy, hot floor, capacity ceiling, GPU driver, queue policy or production code was changed.

## Scope and clocks

Run `trial-customer-20260907-r01`, source `b707b5e0c3fd9252892893eed0008306c21ee873`, ran from **14:54:07.467563Z to 15:20:44.960235Z**, 26m37.493s. The exact runner/scenario/fixture/runtime identities are in [the campaign plan](results-r01/campaign-plan.json), [validated public receipts](results-r01/aggregate.json), and [measurements](results-r01/measurements.json).

There were three sequential operations followed by eleven mixed operations with a maximum of four concurrent public clients. Later cases waited for a local client slot before submission; all 14 operations were **not** durably queued on the server at once. The four-shard RF request is the actual server-side fan-out test.

The table separates authoritative public accepted→terminal time from client wall time (uploads, submission/replay, polling and result downloads), and local slot wait. It is not a GPU-kernel, fresh-node cold-start, scientific-validity or production-SLA benchmark. One or two samples per model do not establish stable percentiles.

| Case | Public accepted→terminal (s) | Client wall (s) | Local slot wait (s) | Result |
|---|---:|---:|---:|---|
| RFdiffusion, switch | 87.860 | 97.679 | — | Passed |
| Protenix v2, switch | 66.506 | 75.559 | — | Passed; actual CUDA-CRIU restore observed |
| Mosaic, switch | 104.201 | 114.191 | — | Passed |
| BoltzGen 1 | 809.384 | 819.344 | 0.000 | Passed |
| BindCraft 1 | 489.014 | 497.107 | 0.001 | Passed |
| Proteina-Complexa 1 | 245.513 | 257.007 | 0.001 | Passed |
| ESMFold2 | 359.479 | 365.903 | 0.002 | Passed after elastic-node wait |
| BoltzGen 2 | 1045.565 | 1052.889 | 257.096 | Passed |
| BindCraft 2 | 462.480 | 468.237 | 365.992 | Scheduler-blocked; explicitly cancelled |
| Proteina-Complexa 2 | 245.399 | 255.868 | 497.195 | Passed |
| Mosaic 2 | 103.743 | 114.096 | 753.085 | Passed |
| RFdiffusion, bulk four shards | 236.724 | 244.596 | 819.426 | Passed after two priority preemptions/retries |
| ESMFold2-Fast | 86.722 | 92.583 | 834.712 | Passed |
| AlphaFold3 | 66.382 | 76.308 | 867.199 | Passed |

For the cancelled case, accepted→terminal is time until cancellation, not inference time or a successful result. Local slot wait is relative to the first mixed worker start at 14:58:54.953016Z and is excluded from the case's client wall time.

## Delivery and lifecycle evidence

- Fourteen initial submissions and fourteen exact idempotent replays returned the same operation identities, without duplicate public operations.
- The campaign client made 969 HTTP calls: 913 HTTP 200, 28 HTTP 201 and 28 HTTP 202. There were no HTTP error responses or transport failures. Explicit cleanup calls are recorded separately, not included in that count.
- Thirteen successful operations passed the existing full model-owned server semantic checks and downloaded a hash-verified result manifest plus one bounded scientific output each: 26 verified downloads.
- Successful operation receipts retain 45 stage attempts: 43 succeeded and two were preempted. These comprise 28 GPU attempts (including the two preempted attempts) and 17 CPU attempts. The cancelled BindCraft reservation is separate and never became a running GPU attempt.
- Every successful, preempted and cancelled attempt reported `resource_released=true` at its observed terminal boundary. Root observation checks subsequent cluster recovery independently.
- Original failure state, manual cancellation and cleanup remain in [the BindCraft failure receipt](results-r01/batch-06-bindcraft.json) and [explicit cleanup record](results-r01/bindcraft2-scheduler-blocked-cleanup.json). The original runner exit status was **1**, not hidden or converted into success.

## What worked

Switching models required the same public submission/status/result interface, without Kubernetes access or deployment edits. Progress remained queryable during long operations. Exact execution identities, stage outcomes, admissions, artifacts and cleanup state were returned consistently.

ESMFold2 initially waited for a preemptible node. The existing autoscaler created capacity within the configured `h100-1x` ceiling and the operation completed. This was genuine, recoverable elastic startup, unlike BindCraft's incompatible node request.

The low-priority RF bulk operation produced all four required outputs under one operation ID. Its `design-001` and `design-002` first attempts were preempted; Kueue evidence correlates higher-priority ESMFold2-Fast and AlphaFold3 requests (priority 0) taking capacity from bulk (priority -100). Both RF shards then succeeded on attempt 2 without client resubmission. These were priority preemptions, not spot-node interruptions.

Protenix's existing policy actually used CUDA-CRIU: observer logs bind operation `e2f0915b-75dd-4673-ab7e-f82c6a551da9` to Pod `7eb0bc7c-521d-429f-af8c-108cc73d21b7`, bundle `protenix-v2-h100-cuda-criu-20260907-r2`, and `mechanism=cuda-criu-restored` at 14:56:30.040255050Z. Merely having a snapshot option is not counted as actual use for other models.

## Blocking issue before an unattended customer trial

BindCraft's second operation selected `h100-1x`. Its scientific container requested 16 CPU and its collector another 0.1 CPU: **16,100m requested versus 15,900m allocatable** on that node flavor. Kubernetes reported `Insufficient cpu`; the autoscaler reported that another identical node would not help. Free capacity on a reserved node did not rescue the operation after the incompatible flavor was selected.

The public operation stayed `running` / `node_pending`, so a customer could wait indefinitely without understanding that this was not ordinary queueing. We cancelled only operation `2ec36323-564e-4a58-9091-9c55e6e7d064` at 15:12:46.188191Z after recording the cause. Cancellation completed and released its reservation.

Before unattended trials, resource-flavor eligibility needs to consider the **complete Pod request**, including sidecars and real allocatable CPU/memory, not GPU compatibility alone. The incompatible BindCraft flavor must be excluded or its resource profile separately validated and corrected. An impossible fit should be reported as an actionable admission/configuration error rather than indefinite `node_pending`. This test did not implement that production change.

## Performance interpretation

BoltzGen 2 was slower but not stuck. Its `design-folding` stage took 395.929s on `h100-1x`, compared with 161.996s on a reserved node in BoltzGen 1: +233.933s, explaining almost all the +233.545s client difference. The other six stage durations were nearly unchanged. Stage elapsed time includes startup and execution; the accompanying observer's phase evidence is needed to attribute that difference more narrowly.

## Workload-lane customer rating

**7/10 for supervised testing; not ready for unattended self-service until the flavor-fit issue is fixed.** The API, results, switching, graceful priority handling and cleanup worked well. One of fourteen operations nevertheless required operator intervention, and its public state did not explain the permanent scheduling mismatch. Waiting for limited capacity is acceptable; silently waiting for capacity that cannot fit the Pod is not.

Offline checks: eight combined original-client/wrapper tests pass; Ruff passes. All model inputs remain the accepted synthetic fixtures, with only the already accepted RF four-shard parameter override and unique customer metadata. This workload report should be read with the root's interactive-service, admin UI and cluster-observer results.
