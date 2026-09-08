# Scientific workload cohort r05

All 14 scientific operations passed, with no client retry, cancellation,
manual recovery or scientific HTTP error. Every attempt reported resource
release. This is the second unchanged scientific pass after r04 on deployed
source `c85aa26e46f84ca5ae0a85b2454e86522b65cead`; root owns the final
cross-lane recovery and two-clean-cohort verdict.

Run `trial-customer-remediation-20260907-r05` lasted from
09:34:48.229430Z to 09:56:40.942742Z on 2026-09-08: **21m52.713s**.
The runner exited 0 naturally; campaign evidence HEAD was
`e597e76342d19cde434f456e9380166bd32bbeb1`.

Original runner/scenario bytes, fixture/input hashes, parameters, priorities and
the four-client ceiling were verified unchanged. The sequential RFdiffusion →
Protenix → mosaic segment preceded eleven mixed requests. Later cases waited
locally for a client slot before submitting; the RF four-shard request is the
actual server-side batch. No production, runtime, model, fixture, policy or
capacity setting changed in this lane.

Evidence: [campaign plan](results-r05/campaign-plan.json),
[aggregate](results-r05/aggregate.json), and
[full measurements, execution identities and trace hashes](results-r05/measurements.json).
Every receipt retains exact runtime image/model/recipe digests and variant ID.
Profile/variant mappings are unchanged from [r03](REPORT-r03.md); no model was
silently substituted.

## Observed customer times

All values are seconds; all rows passed. Accepted→terminal includes server
queueing and execution. Client wall also includes uploads, submit/replay,
polling and two downloads. Local slot wait is before submission and excluded
from the other clocks. These are small-fixture observations under concurrent
normal traffic, not pure GPU execution times, fresh-node cold-start benchmarks,
scientific efficacy results or a production SLA.

| Case | Accepted→terminal | Client wall | Local slot wait |
|---|---:|---:|---:|
| RFdiffusion switch | 77.785 | 86.773 | — |
| Protenix v2 switch | 59.150 | 64.934 | — |
| mosaic switch | 105.948 | 114.043 | — |
| BoltzGen 1 | 810.656 | 821.558 | 0.000 |
| BindCraft 1 | 440.923 | 449.080 | 0.000 |
| Proteina-Complexa 1 | 246.872 | 256.093 | 0.001 |
| ESMFold2 | 135.818 | 142.931 | 0.001 |
| BoltzGen 2 | 841.352 | 851.669 | 143.027 |
| BindCraft 2 | 488.494 | 496.081 | 256.188 |
| Proteina-Complexa 2 | 247.522 | 256.964 | 449.168 |
| mosaic 2 | 105.750 | 114.804 | 706.152 |
| RFdiffusion four-shard bulk | 285.068 | 294.601 | 752.288 |
| ESMFold2-Fast | 83.899 | 92.507 | 820.975 |
| AlphaFold3 | 67.839 | 75.942 | 821.654 |

## Delivery and recovery checks

- Fourteen exact replays returned the same fourteen operation IDs.
- Fourteen server semantic validations passed, including all required RF shards.
- Twenty-eight artifacts were downloaded and hash-verified: one result manifest
  and one bounded scientific output per operation.
- All 894 scientific HTTP calls succeeded: 838×200, 28×201 and 28×202.
  There were 740 status polls; maximum poll response was 0.850696s, maximum
  submit/replay response 0.923368s and maximum download response 0.530897s.
- Forty-six public attempts are retained: 45 succeeded and one was preempted,
  comprising 28 GPU and 18 CPU attempts. Every attempt released its resources.
- Both Proteina admissions/replays and both complete seven-stage BoltzGen
  pipelines finished normally; no alternate operation was created to hide a
  failure or bypass waiting.
- The runner exited normally, and an anchored process check found no owned
  scientific client remaining. Cluster observers continued the recovery window.

The exporter reports a peak of five admitted scientific GPU attempts. This is
not a measurement of total physical cluster occupancy, and the frozen exporter's
generic cancelled-case note does not apply: r05 had no cancellation.

### BindCraft kept its complete resource request

Second BindCraft operation `400bb974-80c1-4cd8-bbc2-c124a147b236` retained
16,100m CPU, 98,560Mi memory and one GPU including its collector, with a
reserved-only selector. Kueue admitted it at 09:43:33Z; it scheduled at
09:43:34Z on reserved node `computeinstance-e00p3acr87k9k4mckj` and was Ready at
09:43:44Z. No prior-cohort capacity wait is attributed to this run. Exact
evidence is private `r05/observer/bindcraft2-fitting-reserved.json`.

### RF batch priority and automatic retry

RF operation `d0bf3a01-d206-4fc4-bd65-0caefc784409` retained four shards at
priority −100. Kueue preempted `design-003` at 09:53:16Z to admit priority0
AlphaFold3 operation `cd8e0e8c-9b10-4a9f-9f62-cb1c5a063a07`.
The exact preemptor Workload UID was `873e3dab-c98f-444f-966b-016c70f58d90`
and Job UID `00558b0a-6278-409f-a62f-26674f2795b7`; retained events and Pod
labels corroborate the priority cause, not spot-node loss.

The preempted attempt released resources. Once the other current shards
finished, attempt2 started automatically at 09:55:02.630171Z and succeeded at
09:56:18.412812Z; the collector completed at 09:56:31.866142Z. The client kept
polling the original operation and passed semantic, download and hash checks.
Private event evidence is `r05/observer/rf-bulk-shard003-preempted.json`;
exact public attempts are in the exported RF receipt. No manual Job operation,
cancellation or client resubmission was needed.

### Protenix used the real GPU snapshot

Protenix operation `2750e2dc-c91f-42cf-a64f-fe8ba4f10c34` used its existing
CUDA-CRIU snapshot. The scientific container started at 09:36:52Z and its
`cuda-criu-restored` marker appeared at 09:36:56.594503293Z. Attempt
`2ea7c586-4688-5175-9bf4-e0a40354a681` reports 4.594503 restore GPU-seconds,
15.405497 active GPU-seconds, 33 scheduler-occupied GPU-seconds and 17.594503
occupied-but-idle GPU-seconds, reconciled with no gaps.

Restore is part of idle occupancy, not an extra quantity to add twice. Its
interval is not pure device-copy time, and end-to-end request time remains the
64.934s client observation above. Actual Pod/runtime evidence is private
`r05/observer/protenix-switch-job.json`. Other models are not claimed to have
used snapshots based only on an available setting.

## Verification and interpretation

All nine offline harness/equivalence/export tests passed; all 42 raw artifact
hashes and all pass/replay/download/resource-release invariants verified. Raw
HTTP traces and access material remain private. Prior failed cohorts and
intermediate failed query tests are unchanged.

Together, r04/r05 delivered 28/28 successful scientific operations, 28 matching
replays, 56 checked downloads and 1,857 successful scientific HTTP calls, with
92 terminal/released attempts (90 succeeded, two preempted). These two small
cohorts demonstrate the exercised workflow and recovery paths, not unlimited
throughput, every possible input or broad long-duration reliability.

Root separately closes normal HTTP/MCP availability, full-window routing logs,
isolated-browser progress/publication/download behavior, accounting
reconciliation and final Kubernetes cleanup before declaring the complete
customer trial ready. No additional scientific traffic is scheduled by this lane.
