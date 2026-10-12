# GROMACS on scientific-ai2 — 12 October 2026

Scope: the new isolated fleet only. This is the first migrated App, not a
customer cutover, full-catalog qualification or protected-patient-data approval.
The existing customer fleet and shared s2pr2 deployment remain unchanged.

## Release and entry points

- Scientific AI source: `9f98aee00` (API r15, workers r10), published on
  `scientific-ai-serverless2-migration`, not main.
- S2 current source: `0f27198912b022a4f9d6d461f0aee656a4043b3c`, API 0.11.31,
  dispatcher 0.2.11, fleet chart 0.1.7, platform chart 0.1.2. The earlier r10 gate used
  `8a964112d3` / dispatcher 0.2.9; 0.1.6 results used `e20013889`.
  Chart 0.1.7 adds Kueue system-CPU flavors using existing regular nodes; its
  paired cohorts and cross-GPU trajectory recovery passed with workers r9.
  A checkpoint-transport failure found in that round required workers r10;
  its fresh unchanged-release qualification passed. Earlier results do
  not silently qualify a later image.
- REST: `https://api.89.169.122.33.sslip.io/scientific-ai/v1`.
- MCP: `https://api.89.169.122.33.sslip.io/scientific-ai/mcp`.
- Console: `https://app.89.169.122.33.sslip.io`.
- Internal workload identity: `system/qa`. No customer key or input was used.

Digest-pinned images in the new registry
`cr.eu-north1.nebius.cloud/e00tgb3jwv3v1mx8g5/scientific-ai`:

| Component | Tag | SHA-256 |
| --- | --- | --- |
| API | pilot-20261012-r15 | 092ae49c072595dcbd61d881125a70a5c722d0d5180bf64591672620afa5a1c7 |
| GROMACS SM89 | pilot-20261012-r10 | 7293a334dd10121dbb7ce4407d06cdeabe64abe66bb29ee333d69468fca912b2 |
| GROMACS SM120 | pilot-20261012-r10 | 6af9d25bb15cc99310f7e15256c3325be6a1e6818772bfbb58673c4ecec99289 |

## Final gate — dispatcher 0.2.11 / platform chart 0.1.2

Completed at **21:46:47 UTC** on the exact release above. The two following
paired REST/MCP cohorts were submitted after the final RBAC apply and dispatcher
leader handover. No deployed image, chart or configuration changed between them.
Each job used one compute attempt; fallback before first execution is not a retry.
The [machine-readable gate](new-fleet-gate.json) preserves receipt provenance.

| Receipt | Public operation | GPU | Steps | Accepted → main | Native command time |
| --- | --- | --- | ---: | ---: | ---: |
| d11-stable-rest | c8dd5763-2bb4-5cd8-bfd4-7ec5387a653d | L40S | 50,000 | 422 s | 118.882 s |
| d11-stable-mcp | ccafd314-ea35-5e81-930c-3e602a9c6b69 | L40S | 100,000 | 362 s | 2.283 s |
| d11-post-c1-rest | b803394b-1651-505e-be0d-f2da43c6b9a0 | RTX PRO 6000 | 50,000 | 60 s | 60.137 s |
| d11-post-c1-mcp | a829531d-bd0d-5cb6-84db-b9e987f92634 | RTX PRO 6000 | 100,000 | 60 s | 3.746 s |

All outputs passed length/SHA256 verification, protein coordinates are finite
with 81,743 particles, and both synthetic trajectories have exactly 101 frames.
L40S was selected automatically after the preferred RTX queue's 60-second wait;
its total startup includes cold capacity and driver/image initialization. The
RTX pair used available capacity. Startup uses the actual main-container attempt
record, not the operation's earlier scheduling timestamp. Short protein rates
were 75.723/78.174 ns/day on L40S and 150.881 ns/day on RTX; these are the original
benchMEM all-bond-constrained input with six CPUs, **not** the tuned Lynx workload
or a production capacity measurement. Synthetic argon is not a biological benchmark.

- Automatic recovery: `fe305a2f-c2ea-57a7-8b34-e5fbd886d2f3` retained its public
  handle after an owned-workload interruption at acknowledged step 570,400.
  Replacement main started on L40S 248.24 seconds after the fault, including cold
  capacity. It finished all 2,000,000 steps; native GROMACS verified **2,001
  continuous frames**, 0–4,000 ps, across three unchanged raw segments, without
  gaps or duplicates. Stale and terminal storage identities returned 403.
  This is an injected interruption, not an observed provider spot revocation.
- Delivery: `ee0822ed-fcbe-560a-b358-72a51c3215d8` completed 100,000 protein
  steps. Concurrent REST/MCP delivery retries produced one finalizer on regular
  CPU node `computeinstance-e07derew8sdahfcqwk`, zero GPUs, unchanged three native
  command receipts and native-result SHA256
  `ca55981c2053d257d1fc0d7f4477ff107c37d910792dae05121ff4c25ae7c048`.
  Exactly one GPU compute attempt; no scientific rerun. CPU main took 52 seconds,
  not end-to-end delivery latency. Successful delivery scratch was removed.
- Cancellation: `d48ac5f9-b8a6-5197-b45e-e745e708e00d` acknowledged MCP cancel
  via REST in **1.224 seconds**; the actual worker was removed in **127.737
  seconds**, including configured 120-second grace. Its previously accepted
  projected identity could not obtain new storage URLs (403). Already-issued
  URLs retain their documented maximum 60-second TTL.
- Cleanup: three superseded claims stayed absent for at least 60 seconds, with
  unchanged manager Job UIDs, public handles, attempt IDs and native-result
  hashes. Failed-delivery storage remained intact. Four final Terraform drift
  plans are empty; the preserved customer website/API/MCP passed all 15 checks.

The original `d11-final` and `d11-stable` drivers recorded offline-observer
failures: their image UID could not read private mode-0600 host receipts. Commit
`81878fbb2` fixed the reader UID without loosening permissions or changing any
deployed image. Separate observer rechecks passed against **the same outputs**,
without GPU reruns. Original failed aggregate receipts remain failed; the fresh
`d11-post` driver passed directly. The first `d11-final-c1` pair spans leader
handover and is retained as rollout/cold-start evidence, not a final paired cohort.
Recovery and delivery computation/faults occurred after the final RBAC apply.

At closeout, neither worker namespace had any test Pod. Two cancelled-run PVCs
(`op-89619e6b95cf6c2c-work`, `op-0e79c6eafad278be-work`) and failed-delivery
fixture `op-b9bd5fd41eabe2af-work` remain under the existing resume/failed-delivery
retention policy; these contain only owned system/qa acceptance data. Their
cleanup disposition belongs to the operator after evidence retention, not an
invented TTL or deletion of durable history. Shared weights/scratch are untouched.

## Earlier chart 0.1.7 qualification and checkpoint-transport regression

All four `chart17-c{1,2}-{rest,mcp}` runs passed on workers r9. The two protein
runs completed 50,000 steps and all artifact checks; both synthetic runs
completed 100,000 steps and 101 valid frames. `chart17-recovery-mcp`, public
`2ddac966-22ad-5202-853b-80ee0544b718`, checkpointed at step 509,500 on RTX,
automatically resumed on L40S, completed 2,000,000 steps and returned exactly
2,001 continuous frames across three raw XTC segments. Native `gmx check`,
file hashes, stale-worker denial and terminal storage denial passed.

The `chart17-delivery-mcp` gate **failed**, not passed: public
`ac416fff-9745-5d37-ad20-af94668f807a` completed native GROMACS successfully
(all commands exit 0, final step 50,000, 171.488 ns/day), but a temporary DNS
failure during its final checkpoint publication made the native workflow's
overall result `failed`. S2 consequently ran a second GPU attempt; that attempt
then hit the intended lost-ack fault. This was an application error-classification
gap, not bad physics or proof of CPU delivery. Original receipts and read-only
scratch inspection are retained; no failed evidence was overwritten.

Workers r10 add bounded immutable-transfer retries (including raw DNS failures)
and distinguish final checkpoint transport from failed computation. Only after
all native commands, requested steps and output checks finish successfully can
that specific transient publication failure enter retained CPU delivery. Denial,
corruption, incomplete/interrupted work, native errors and incomplete inventory
remain failures. The unacknowledged checkpoint generation never becomes a
committed head. Six new regression cases cover this distinction. This is not
a live S3-outage qualification; the final fleet fault still tests lost ack.

All `r10-final-*` gates passed at 21:07:33 UTC: two paired cohorts, native
cross-GPU recovery, CPU-only delivery and cancellation, with no intervening
S2/chart/image change. This is scoped internal GROMACS acceptance, not full
customer migration or production-capacity qualification.

| Earlier r10 receipt | Public operation | Native steps | Acceptance → main | Native command time |
| --- | --- | ---: | ---: | ---: |
| r10-final-c1-rest | 589afaca-09ee-5f5a-9012-b68d1454b997 | 50,000 | 67 s | 57.564 s |
| r10-final-c1-mcp | 8a8338d8-34d0-50ea-a894-52bf4b9c2fb0 | 100,000 | 64 s | 2.937 s |
| r10-final-c2-rest | f6f07ab1-8ac1-5d5f-a07b-c6fe7e299c78 | 50,000 | 109 s | 66.077 s |
| r10-final-c2-mcp | dcb7b6e1-72e6-5ab6-ba49-cd49dd990cd8 | 100,000 | 231 s | 3.809 s |

Each used one compute attempt. All 10/13/13/13 artifacts passed content checks;
both protein final structures are finite with 81,743 particles, both synthetic
trajectories contain 101 valid frames. The second protein run spans two native
segments, not two GPU attempts. These are functional acceptance/queue timings,
not tuned benchmarks or production capacity bounds. The longest startup
includes contention while spot nodes scale down/replace; no pool bound was raised.

`r10-final-recovery-mcp`, public `3bf30343-043f-5737-897e-7a9f9a6648e1`, also
passed. An acknowledged checkpoint at step 509,300 moved automatically from
RTX/SM120 south to L40S/SM89 north, preserving the public handle. It finished
2,000,000 steps with 22 verified files and exactly 2,001 continuous XTC frames
(0–4,000 ps at 2 ps intervals, three untouched raw segments). Native commands
total 45.179 seconds; the two durable attempt-record intervals total 144 seconds,
not measured physical GPU allocation/release. Fencing can end an attempt record
before its terminating Pod releases the GPU. Stale identity fails, the successor works,
and new storage URLs are denied after terminal completion.

`r10-final-delivery-mcp`, public `1df9d361-5888-5bba-a5bd-df5a3b085360`,
completed 100,000 protein steps, then hit the deliberately unwritable final-ack
path and exited 75. There was exactly one GPU compute attempt. Concurrent REST
`:export` and typed MCP retry created exactly one finalizer, epoch 2, on regular
CPU node `computeinstance-e07derew8sdahfcqwk` (`pool=system`, zero allocatable
GPUs and zero GPU requests). It delivered the original native-result hash
`9aa0b6b122e09851233120d504ba5142d8141a49e14052d5652070d1b5bb96a7`
and exactly the original three command receipts, then removed its retained PVC.
Finalizer main ran 21:03:55–21:04:27 UTC; completion/content checks finished at
21:04:47. GPU main had ended at 21:00:39: the intervening delay includes the
controlled repair helper, regional volume detach/attach and CPU-pod startup.
This is not 32 seconds of end-to-end delivery latency, nor a denied-S3-write drill.

`r10-final-cancel`, public `21abeaf5-f450-532c-abad-83fae5323b1e`, received
MCP cancellation while running. REST acknowledged CANCELLED in 1.30 seconds;
its worker Pod was removed after 127.40 seconds, including the configured
120-second grace. The real previously accepted pod identity was denied new
storage URLs (403). No workload test remains active after the suite.

## Unchanged-release cohorts — API 0.11.31 / chart 0.1.6

Two cohorts each submit REST protein and typed-MCP trajectory jobs concurrently.
Every returned artifact is downloaded and verified by SHA-256 and length.

| Receipt | Public operation | Steps | Acceptance → main | Native command time |
| --- | --- | ---: | ---: | ---: |
| final-c1-rest | 8219718e-2889-5324-b511-a2c29fa83765 | 50,000 | 57 s | 57.270 s |
| final-c1-mcp | e19daa36-5ed1-5648-9ee2-e2dc917e25cd | 100,000 | 174 s | 3.738 s |
| final-c2-rest | 254e33ae-7fcc-5a02-8a91-7bc56b66b975 | 50,000 | 51 s | 60.106 s |
| final-c2-mcp | 63cad141-b170-5783-b104-2481f54e079e | 100,000 | 113 s | 3.847 s |

All four succeeded on RTX. Protein runs use the public, SHA-pinned MPINAT
benchMEM TPR: 81,743 atoms, PME, 2 fs, original coordinates/velocities and
all-bond constraints requiring CPU update. Preparation executes once; final
coordinates are finite and retain the correct particle count. Native rates
158.749 and 151.120 ns/day are short six-CPU acceptance measurements, not tuned
Lynx-workload performance or measured multi-customer capacity.

The independent synthetic case is 4,096-atom argon, not a biological result.
Native GROMACS `check` verifies all 101 XTC frames (0–200 ps, 2 ps interval) for
both cohorts. The protein TPR disables trajectory/energy output: it does not
establish ensemble convergence or trajectory continuity.

Earlier diagnostic cold-node runs also succeeded: `fleet-fixed-rest` and
`fleet-fixed-mcp` took 437 and 446 seconds from admission to main startup.
They include spot provisioning, driver/device-plugin setup, disk attach and
image initialization and span an S2 rollout. They are not clean-cohort evidence
or checkpoint-restore timings. The 51–174 second cohort timings include queue
contention and disk attachment; they are not a startup SLO.

## Recovery, delivery and isolation

- `fleet-recovery-rest`, public `2fce1b04-bc51-5b6b-8dcb-7b9e65446ac1`:
  a real acknowledged checkpoint at step 76,700 preceded deactivation of only
  its owned Kueue Workload. The same public ID automatically moved from RTX
  south / SM120 to L40S north / SM89, epoch 1 → 2. No manual resume call or
  node deletion. The original pod token paired with the successor ID is denied
  (403); the successor identity is accepted (200). Completed all 250,000 steps
  with 28 verified files, one preparation and finite 81,743-atom final
  coordinates. Fault at 20:18:41.744 UTC; successor main at 20:23:46 (304.256 s,
  including cold L40S provisioning/driver setup); durable completion 20:31:14.
  Recorded native commands total 415.712 s. Durable attempt-record intervals
  total 649 seconds, including 215 s on the interrupted RTX attempt. These
  records are not proof of physical GPU release; they omit scheduling/init
  reservations and may omit later draining. This is neither complete allocation
  accounting nor native useful-work time. The same logical operation
  retained both attempts and their different image digests.
- `fleet-delivery-mcp`, public `c67d8108-b050-5100-b084-a94f45b3707e`:
  completed all 250,000 protein steps, then deliberately failed its final local
  acknowledgement. Exit 75 correctly failed the compute Job without a second
  GPU attempt. Concurrent REST `:export` and MCP `retry_scientific_delivery`
  created exactly one finalizer. It delivered 19 verified files, the identical
  native-result hash and the same five native command receipts. Zero GPUs were
  requested. Its retained scratch PVC was automatically removed after success.
  **0.1.6 gap:** Kueue still placed its CPU resources on a GPU pool. Chart 0.1.7
  is now deployed to correct this; regular-CPU delivery passed in the r10 gate
  above. This drill tests acknowledgement failure, not an S3 outage.
- `fleet-cancellation`, public `7c039636-a260-56ca-8229-d99a7d47e741`:
  running-job MCP cancellation acknowledged in 1.33 s; REST reports CANCELLED,
  the GPU pod disappeared in 127.43 s including the 120 s termination grace,
  and its previously accepted real identity cannot obtain new storage URLs.
- Anonymous REST/MCP: 401. Another internal principal's operation/artifact:
  404. Six concurrent idempotent replays: one operation. Changed intent with
  the same key: 409. Unsupported resume options: 422.
- Real 129 MiB multipart upload and download matched SHA-256; upload 4.188 s,
  readback 2.784 s. Missing/forged pod identities return 403. Terminal delivery
  operations return 403 for new object URLs; already issued URLs can remain
  usable for their documented maximum 60-second lifetime.

## Source defects found and fixed

Real GROMACS requests exposed missing cluster-alias resolution in private-input
delivery, confusion between authenticated cluster IDs and physical regions,
ambiguous finalizer placement, and GPU retries after delivery-only exit 75.
These were fixed with source regressions and narrow new-fleet Terraform applies.
See [NEW-FLEET-COORDINATION.md](NEW-FLEET-COORDINATION.md). The initial failed
job is retained in [new-fleet-baseline-failure.json](new-fleet-baseline-failure.json),
not counted as scientific success.

The delivery observer initially tried to read a worker Pod after MultiKueue
had correctly deleted it, then used a nodeName incompatible with Kueue's
scheduling gate. Only the observer was corrected: durable attempt records
replace ephemeral-Pod exit lookup; its bounded repair helper uses ordinary
Kueue admission. The same completed calculation was reused, not rerun.

Local application checks: 167 passed, one separately enabled real-native test
passed on the exact r10 SM89 digest (600,000 steps; preparation once), ruff passed.
The reused legacy GROMACS worker regression selection also passes: 75 tests.
The final application-plus-observer rerun passed 169 tests (167 application,
two offline-reader regressions); its one opt-in native test is skipped by that
default command and is covered by the separate exact-image run above.
An initial native-test invocation with an unpinned tag was rejected before any
simulation; rerunning with the required digest passed. Four final fleet drift
plans for the earlier r10 gate at source 8a964112d report
zero changes. The old public IP passed all 15 website/API/MCP routing checks;
`forge.nebius.cloud` was not changed.

## Reproduction and evidence

Use `pilot.py --deployment ../../deployments/scientific-ai2/gromacs-target.json`
from `components/scientific-ai`; all actions require an explicit private
receipt directory. Actions cover submit/status/wait, checkpoint recovery,
replay, cancellation, storage identity, terminal denial and native trajectory
validation. `delivery_retry.py` injects only an owned internal-job fault and
uses the actual public REST/MCP retry paths. Do not use customer identities.
`qualify.py` runs two paired cohorts and independent recovery/delivery branches
concurrently, retaining each failed gate instead of treating aggregate progress
as success. Use a fresh receipt prefix; it refuses to overwrite earlier evidence.

Private receipts, raw outputs, timelines and Terraform plans are retained at
`/home/tux/secure-handoff/scientific-ai2-gromacs-20261012.faTMbM` (0700).
Do not publish config.json, plans/state, projected tokens or signed URLs.

No customer keys/buckets/history were migrated. This qualifies neither all
GROMACS/MPI protocols nor other models, LibreChat, external AWS credentials,
HIPAA compliance, HA PostgreSQL or production-scale concurrency. GROMACS uses
portable native checkpoints, **not GPU-memory snapshots**. Keep the fleet up;
only exact owned test helpers and obsolete scratch may be cleaned.

## Closed follow-up: superseded recovery-chain scratch

S2's dispatcher recreates a deleted old root PVC when its superseded manager
Job remains suspended. The empty `op-a71d0a302dae82c2-work` reappeared at
20:45:01 after its successor had already succeeded. Durable history survives
in S2, but this old scratch should not persist/reappear indefinitely. This is
tracked in Agent Task Deck `fs2-s2-closed-recovery-scratch-retention-r20261012`.
The source fix and new-fleet-only acceptance are complete as recorded below.
Do not erase active resume/export
scratch, add arbitrary retention limits, or repeatedly delete a reconciled PVC.
The passed r10 execution gate above remains versioned evidence, not a claim
that this newly discovered lifecycle gap was already fixed in dispatcher 0.2.9.

The first proposed cleanup candidate, source `fc18172eb` / dispatcher 0.2.10,
was **rejected before deployment**. Diff review found that it inadvertently
removed `load_fleet` while `reconcile` still called it. Its 266 passing helper
tests did not exercise that entry point. The owner was asked to restore it,
add real reconcile-entry regression coverage and publish a new version. The
new fleet stayed on 0.2.9; no customer or shared deployment received 0.2.10.

The corrected source `6d8c804fb` / dispatcher 0.2.11 restores the function and
adds whole-reconcile regression tests. Its source gate passed 268 tests
(13 separately gated skips), chart renders and four Terraform tests; an
independent run of all 41 dispatcher tests and undefined-name lint passed.
Only its image was mirrored into the new registry, with matching OCI index
`sha256:859828699ebaf97abfc9c065cb12a1ca97afbac191b249e0dc6702eae6e1dad9`.
The reviewed saved Terraform plan contained exactly two image-only updates:
dispatcher Deployment and price-feed CronJob in new control `kueue-system`.
No resource creation/deletion or other component update. It was applied at
21:28 UTC; old dispatcher replicas drained for 30 seconds and the new leader
waited for the normal 60-second lease expiry. The first `d11-final` requests
were initially nominated by the draining old leader at 21:28:27, so that first
pair is rollout evidence, not a strictly post-handover cohort. Separate
post-handover requests supplement the final-release gate; raw evidence remains.

Live reconciliation then exposed missing chart RBAC: the dispatcher had
`get`, but the new reference enumeration requires `list` on `batch/jobs`.
At 21:30 UTC the controller correctly preserved scratch on that 403; no
operator credentials or authorization bypass were substituted. Admission
continued, but cleanup was not accepted. The source owner added that
read-only permission and a rendered-RBAC regression; the image did not
need rebuilding. The initial four no-drift plans are evidence of IaC/live
agreement, **not** proof that its permissions were sufficient.

Platform chart 0.1.2, source `0f2719891`, was applied at 21:34 UTC after
reviewing a saved plan with exactly one change: add `list` to the dispatcher's
existing read-only `batch/jobs` rule. Actual authorization now reports `yes`;
no application restart or image change was required. At 21:34:06 the
dispatcher deleted the three obsolete south claims rooted at
`op-85a5a7282c33f890`, `op-a71d0a302dae82c2`, and `op-bb7a2857ff187901`.
The read-only checker passed at 21:35:53: all stayed absent for at least 60
seconds, all three root Job UIDs/public handles/attempt IDs/native-result
hashes were unchanged, and failed-delivery claim `op-b9bd5fd41eabe2af-work`
retained its exact UID. Durable objects were not deleted; this is not proof
of physical disk erasure. The pre-fix timeout remains in
`dispatcher11-scratch-check.log`; the pass is in
`dispatcher11-rbac-scratch-check.log` and `dispatcher10-scratch-after.json`
(the latter deliberately retains its original baseline prefix).

All four repeated fleet plans at `0f2719891` report no drift. No bound, quota,
hot floor, customer resource or old/shared-fleet route changed.

The first local trajectory observer in this round also failed: the GPU job
completed, but the offline CPU checker used the image's service UID against
host receipts created with mode 0600. Native `gmx check` correctly reported
file access denied. Acceptance commit `81878fbb2` runs both offline readers
as the operator's UID/GID, keeps the private permissions and stores native
diagnostics before raising. Two regression cases cover success (including
overlapping-frame comparison) and retained failure diagnostics. Rechecking
the **same unmodified** `d11-final-c1-mcp` output passed all 101 frames and
API ownership/replay checks at 21:40:05, with no new GPU execution. Earlier
processes retain their original observer failures; do not relabel their
aggregate receipts as passed. Subsequent offline rechecks and fresh cohorts
are recorded separately. This change affects no deployed image or chart.
