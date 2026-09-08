# Scientific workload cohort r04

All 14 scientific operations passed on the final startup-retention release,
without a client retry, cancellation, manual recovery or scientific HTTP error.
Every attempt reported resource release. Root owns the cross-lane recovery
verdict and authorization for the next cohort; this report records the complete
scientific client evidence, not an inferred platform-wide SLA.

Run `trial-customer-remediation-20260907-r04` lasted from
09:01:13.093359Z to 09:27:30.125076Z on 2026-09-08: **26m17.032s**.
The runner exited 0 naturally. The deployed release remained
`c85aa26e46f84ca5ae0a85b2454e86522b65cead`; campaign evidence HEAD was
`ba94d71bc1dafc84870be760959abcecd18a3c84`.

The wrapper verified unchanged original runner/scenario bytes, fixture and input
hashes, parameters, priorities and the four-client ceiling. Three sequential
requests (RFdiffusion → Protenix → mosaic) preceded eleven mixed requests.
Later clients waited locally before submission. The RF four-shard operation is
the actual server-side batch, not the four-client executor. No production,
fixture, runtime, policy or capacity setting changed in this lane.

See the [campaign plan](results-r04/campaign-plan.json),
[aggregate](results-r04/aggregate.json), and
[per-operation measurements, identities and trace hashes](results-r04/measurements.json).
Each operation receipt includes its exact runtime image/model/recipe digests and
variant identity; no requested model was silently substituted. Profile/variant
mappings remain those documented in [r03](REPORT-r03.md).

## Observed customer times

All values are seconds and all rows passed. Accepted→terminal includes server
queueing and execution. Client wall additionally includes uploads,
submission/replay, polling and two downloads. Local slot wait precedes submission
and is excluded from the other clocks. These are small-fixture customer
observations under normal background traffic, not pure GPU execution times,
fresh-node cold-start benchmarks, scientific efficacy evidence or an SLA.

| Case | Accepted→terminal | Client wall | Local slot wait |
|---|---:|---:|---:|
| RFdiffusion switch | 87.777 | 97.638 | — |
| Protenix v2 switch | 64.142 | 70.582 | — |
| mosaic switch | 104.916 | 114.106 | — |
| BoltzGen 1 | 914.527 | 924.478 | 0.000 |
| BindCraft 1 | 390.188 | 399.335 | 0.001 |
| Proteina-Complexa 1 | 252.961 | 263.749 | 0.001 |
| ESMFold2 | 336.044 | 344.100 | 0.001 |
| BoltzGen 2 | 1021.322 | 1030.785 | 263.842 |
| BindCraft 2 | 383.842 | 389.321 | 344.190 |
| Proteina-Complexa 2 | 247.177 | 255.902 | 399.428 |
| mosaic 2 | 107.503 | 114.296 | 655.351 |
| RFdiffusion four-shard bulk | 315.150 | 321.188 | 733.530 |
| ESMFold2-Fast | 86.407 | 92.385 | 769.666 |
| AlphaFold3 | 67.235 | 75.802 | 862.071 |

## Delivery and lifecycle checks

- Fourteen exact replays reused the same fourteen operation IDs; both Proteina
  initial submissions and replays completed normally.
- Fourteen server semantic validations passed, including all required RF shards.
- Twenty-eight artifacts were downloaded and hash-verified: a result manifest
  and one bounded scientific output per operation.
- All 963 scientific HTTP calls succeeded: 907×200, 28×201 and 28×202.
  There were 809 status polls; maximum poll response was 1.172325s, maximum
  submit/replay response 0.751643s and maximum download response 0.552366s.
- The public receipts retain 46 attempts: 45 succeeded and one preempted,
  comprising 28 GPU and 18 CPU attempts. Every attempt released its resources.
- The runner terminated normally. An anchored process check confirmed no owned
  scientific client remained active; observers continued their recovery window.

The exported peak-admitted-attempt count is 5 and is derived from scientific
receipts, not total physical cluster occupancy. Its inherited historical
cancelled-case caveat does not apply to r04: there was no cancellation.

### Full-Pod BindCraft placement remained correct

Second BindCraft operation `f99f408d-384c-48f9-b86d-9d057983a842` retained
16,100m CPU, 98,560Mi memory and one GPU for its complete Pod, including the
collector. The reserved-only selector placed it on reserved node
`computeinstance-e00p3acr87k9k4mckj`: admission/scheduling at 09:11:46Z, Ready at
09:11:56Z. There was no capacity wait beyond ordinary admission in this case.
Neither request resources nor pool limits were lowered or raised to pass.

### RF priority preemption and automatic recovery

RF operation `f7963270-82af-4878-8942-96bdd1841a7a` retained four shards and
priority −100. Exact observer Kueue events recorded `design-001` displaced by a
natural priority0 Qwen burst at 09:18:27Z, and `design-002` displaced by
priority0 ESMFold2-Fast at 09:19:03Z. These were priority preemptions, not proven
preemptible-node loss.

The public ledger records one preempted attempt: `design-001` attempt1 released
resources, then attempt2 started automatically at 09:21:55.211227Z and succeeded
at 09:22:58.882353Z. The collector finished at 09:23:23.160230Z. The client
continued polling the original operation and passed all validation/download
checks without resubmission.

`design-002` remained a successful public attempt1 despite its separately
observed physical Pod recreation. Two Kueue preemption events must therefore
not be reported as two public retries. Both evidence levels are retained.

### Actual Protenix snapshot use

Protenix operation `1b0d573b-0ce9-440c-9141-1f0156bbad36` actually used its
existing CUDA-CRIU snapshot. Its scientific container started at 09:03:31Z and
the `cuda-criu-restored` runtime marker appeared at 09:03:35.315065492Z.
The reconciled `sample-structure` attempt
`022bcf95-fdd5-5869-83aa-d8afa4166b9e` reports 4.315065 restore GPU-seconds,
17.684935 active GPU-seconds, 37 scheduler-occupied GPU-seconds and 19.315065
occupied-but-idle GPU-seconds, with no gaps. Restore is included in idle; do not
add it twice or treat the interval as pure device-copy bandwidth.

Exact Pod/runtime evidence is in private `r04/observer/protenix-switch-job.json`
and the terminal ledger in observer samples. Other models are not claimed to
have used snapshots merely because an option exists.

### The slower BoltzGen run progressed normally

BoltzGen2 completed all seven stages, without retries or an unexplained terminal
stall. Its `design-folding` attempt lasted 371.122s versus 173.137s for BoltzGen1;
both used the `h100-1x` pool on different preemptible nodes.

The observer's existing ledger attributes the main difference to fitting-slot
wait and a cold image: run2 quota occupancy was 371.688690 GPU-s versus 250
scheduler-occupied GPU-s, including 90 image-pull GPU-s and 151 active GPU-s.
Run1 was 174.431114 quota GPU-s versus 170 scheduler GPU-s, including 1 image-pull
GPU-s and 159 active GPU-s. The approximately 121.69s versus 4.43s pre-placement
differences are estimates from distinct clocks, not an exact additive
decomposition of client or stage wall time. These observations do not indicate
slower GPU compute in run2.

## Evidence checks and next cohort

All nine offline harness/equivalence/export tests passed. All 42 raw artifact
hashes and the 14-pass/replay/download/resource-release invariants verified.
Original inputs and harness remain unchanged; access material and raw HTTP
traces remain private. Prior failed cohorts were not rewritten.

Root separately owns the actual natural Qwen cold-start/restore/cleanup proof,
isolated-browser publication/download checks, normal HTTP/MCP continuity and
complete recovery verdict. R05 remains prepared but unstarted until explicit
authorization after r04 closure.
