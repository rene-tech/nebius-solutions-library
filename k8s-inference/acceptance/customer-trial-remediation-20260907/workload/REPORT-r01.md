# Scientific workload rerun r01

All **14 scientific operations passed**, without manual intervention or client
retry. The former BindCraft placement failure is repaired in this live run.
However, this is **not an overall clean customer-experience cohort**: independent
observation found an incorrect admin phase-duration display and a brief Qwen
route-withdrawal gap during burst scale-down. Those findings must remain in the
parent verdict even though this scientific client saw no HTTP errors.

Run `trial-customer-remediation-20260907-r01` lasted from
**16:08:17.058637Z to 16:33:09.949657Z**, **24m52.891s**, on 2026-09-07.
The runner exited **0**. See the [deployed release](../RELEASE.md),
[exact campaign plan](results-r01/campaign-plan.json),
[aggregate receipts](results-r01/aggregate.json) and
[full timing measurements](results-r01/measurements.json).

The wrapper verified that the original runner bytes, scenario order, fixture
hashes, input digests, parameters, priorities and four-client ceiling were
unchanged. There were three sequential operations, RFdiffusion → Protenix →
mosaic, followed by eleven mixed operations. Only four clients submitted at a
time; later cases waited locally for a client slot. The RF four-shard request,
not the local thread pool, is the actual server-side batch test.

## Observed customer times

All values below are seconds. Public accepted→terminal time includes accepted
server queueing and execution. Client wall additionally includes uploads,
submission/replay, polling and two result downloads. Local slot wait occurs
before submission and is excluded from both other clocks. These are tiny-fixture
customer observations under shared-cluster traffic, not GPU-kernel timings,
fresh-node cold-start measurements, scientific validation or an SLA.

| Case | Accepted→terminal | Client wall | Local slot wait | Outcome |
|---|---:|---:|---:|---|
| RFdiffusion switch | 78.190 | 86.808 | — | Passed |
| Protenix v2 switch | 61.751 | 70.256 | — | Passed; actual GPU restore |
| mosaic switch | 104.478 | 114.207 | — | Passed |
| BoltzGen 1 | 812.016 | 818.453 | 0.000 | Passed |
| BindCraft 1 | 672.523 | 678.297 | 0.000 | Passed |
| Proteina-Complexa 1 | 243.922 | 251.882 | 0.001 | Passed |
| ESMFold2 | 353.396 | 360.366 | 0.002 | Passed after elastic-node wait |
| BoltzGen 2 | 897.321 | 906.472 | 251.981 | Passed |
| BindCraft 2 | 586.369 | 594.455 | 360.465 | Passed after valid reserved-pool wait |
| Proteina-Complexa 2 | 246.343 | 256.525 | 678.394 | Passed |
| mosaic 2 | 104.206 | 114.455 | 818.546 | Passed |
| RFdiffusion four-shard bulk | 282.473 | 288.522 | 933.020 | Passed after three priority preemptions |
| ESMFold2-Fast | 84.441 | 93.021 | 934.939 | Passed |
| AlphaFold3 | 73.431 | 81.746 | 954.947 | Passed |

Each receipt retains the exact runtime image digest, model revision, execution
identity, recipe digests and variant ID. Requested profile labels map to these
observed variants; they are not substituted silently:

| Profile | Validated runtime variant |
|---|---|
| Proteina-Complexa | `upstream-dev-20260827` |
| BoltzGen | `upstream-v0-3-2` |
| mosaic | `mosaic-boltz2-proteinmpnn-v1` |
| BindCraft | `v1-5-3-pyrosetta-academic` |
| RFdiffusion | `rfdiffusion-v1-1-0` |
| ESMFold2 / ESMFold2-Fast | `biohub-v3-4-0`; separate image/execution digests |
| Protenix v2 | `upstream-v2-0-0` |
| AlphaFold3 | `upstream-v3-0-4` |

## Delivery, queueing and recovery

- Fourteen exact submission replays reused the same fourteen operation IDs.
- All fourteen server semantic validations passed. The client downloaded and
  hash-verified 28 artifacts: one result manifest and one bounded scientific
  output per operation. RF's semantic checks covered its four required shards.
- The scientific client made 1,004 HTTP calls: 948×200, 28×201 and 28×202. No
  HTTP or transport failure was observed, and there was no cancellation call.
- Receipts retain 48 stage attempts: 45 succeeded and three were preempted.
  There were 30 GPU attempts, including the three preempted attempts, and 18 CPU
  attempts. Every terminal attempt reported its resources released.
- The workload runner and its public clients terminated normally. Independent
  samplers own cluster recovery and normal-traffic evidence, not this client.

### BindCraft's impossible placement is fixed

The second BindCraft operation
`b5c5aa76-ae94-4fd6-875f-4fd7fccee0fe` kept its exact 16CPU/96Gi stage request
plus the 100m/256Mi collector. Its frozen affinity admitted only
`h100-reserved-8x`, excluding the 15,900m-CPU `h100-1x` node.

It waited legitimately for reserved-pool GPU quota after acceptance at
16:18:52.267157Z. Kueue admitted it at 16:22:15Z, and the Pod was scheduled at
16:22:16Z on `computeinstance-e00p3acr87k9k4mckj`. This approximately 204-second
acceptance→scheduling interval is not a stalled impossible Pod or client-slot
wait. The original pending failure is preserved separately in the pre-remediation
campaign; it was not reclassified or overwritten.

The new request completed its GPU design and CPU aggregate stages normally,
without a retry, resource reduction, quota increase, cancellation or operator
placement override. The admission and per-stage timing are in its
[public receipt](results-r01/batch-06-bindcraft.json); the independent observer
retains the exact Job, Pod, affinity, resources and node-capacity evidence.

### The bulk request yielded to other work and recovered

RF operation `ca30eecd-6aa2-49d2-9472-1c2a3d185876` retained these first-attempt
preemptions, corroborated by Kueue events:

| Shard | Time UTC | Higher-priority preemptor |
|---|---|---|
| `design-000` | 16:28:42 | ESMFold2-Fast |
| `design-003` | 16:28:44 | Normal Qwen burst Pod |
| `design-001` | 16:29:06 | AlphaFold3 |

The scientific preemptors used priority 0 versus the RF bulk priority −100.
These were policy-priority preemptions, not spot-node loss. All three shards
restarted automatically on attempt 2 and succeeded; `design-002` succeeded on
attempt 1. The customer kept polling the original operation ID. No hidden
client resubmission was used to obtain a passing result.

### Snapshot evidence is actual use, not just availability

Protenix operation `18efdb3c-fa99-42a7-9a39-876d22a2021b` used its existing
CUDA-CRIU snapshot. The observer captured the successful restore marker at
16:10:27.946846795Z with `mechanism=cuda-criu-restored` and return code 0.
Its reconciled GPU ledger records 3.946846 restore GPU-seconds, 16.053154 active
GPU-seconds, 33 scheduler-occupied GPU-seconds and 16.946846 idle GPU-seconds.
Restore is a cause within occupied-but-not-active time; do not add it to idle
again. The restore interval includes supervisor preparation, not only CUDA copy.

The separate admin phase-duration card incorrectly showed 0s measured while its
ledger card showed the correct 3.95 GPU-s. The UI used ingestion timestamps for
backfilled lifecycle events. This defect is preserved in the
[browser observations](../experience/r01-browser-observations.json); nothing was
deployed mid-cohort to hide it. Other models are not claimed to have used GPU
snapshots merely because an option exists.

### Performance interpretation

The second BoltzGen run was 88.019s slower in client wall time. Its
`inverse-folding` stage took 124.304s on `h100-1x` versus 35.194s on reserved
capacity in the first run, accounting for nearly all that difference. This is
stage elapsed time, including startup and execution; the separate observer's
phase evidence is required to attribute it more narrowly. Neither pipeline was
stuck, and both completed all seven stages.

## Remaining acceptance work

The scientific workflow lane passed this bounded cohort. That does not erase
the admin phase-duration defect, ambiguous global freshness label, or the
observer's approximately 5.2-second Qwen route-withdrawal gap during burst
scale-down. Zero sampled HTTP failures do not prove there was no routing gap.
Root owns those cross-lane findings, deployment and the final experience rating.

Two consecutive clean complete cohorts after the final fix are still required.
No second cohort was started automatically. All raw HTTP traces and sensitive
access material remain private; exported receipts and trace hashes contain no
credentials. The original failed campaign remains unchanged.
