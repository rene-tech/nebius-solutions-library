# GROMACS on Serverless 2: live pilot result

11 October 2026, shared **s2pr2** fleet only. Runtime source: `73414081c` on
`scientific-ai-serverless2-migration`; driver and evidence updates accompany this
report. **The internal single-GPU pilot works. This is not customer cutover or
production/security approval.** No customer cluster, key, bucket, job or DNS was
changed. No Serverless source, shared pool floor or fleet configuration was changed.

## Deployment

- REST: `https://api.195.242.30.226.sslip.io/scientific-ai/v1`
- MCP: `https://api.195.242.30.226.sslip.io/scientific-ai/mcp`
- Health: `https://api.195.242.30.226.sslip.io/scientific-ai/health`
- Internal tenant/key alias: `sciai` / `sciai-worker`. No credentials in this report.
- S2 model: `scientific-gromacs-pilot`; public Scientific AI App ID: `gromacs`.
- Initially API 0.11.11, uploader 0.1.14, inspected S2 source `56aaa9eb3`.
  Other fleet lanes rolled API 0.11.12 at 15:37:29 UTC and 0.11.13 at 15:48:28.
  Final inspected S2 source: `fdcbf7785`. All three contract probes also pass
  against it. Scientific AI's r5 images/profile did not change during these rolls.
- API namespace/Deployment/Service/HTTPRoute: `scientific-ai-pilot`, control cluster.
- Runtime revision: `gromacs-2026-4-pilot-20261011-r5`; native checkpoint
  compatibility ID: `gromacs-2026-4-native-v1`.

Digest-pinned images, all under `registry.serverless2.local/nebius/scientific-ai/`:

| Image | SHA-256 |
| --- | --- |
| `api` | `3172bd448f88d0b7a3b305998b900754a8fe62a12a947ceb7bd2077b86fb6db3` |
| `gromacs-sm120` (RTX PRO 6000) | `5908c717a8f608a7b23055dc1fcd8ef544fe74bf95e3aba24d9d716c952c9d54` |
| `gromacs-sm89` (L40S) | `170e6bc4aa6fea86d79d5db70332703a17fccb786b624004983498769cfaf947` |

GROMACS 2026.4, CUDA 12.8.1, native SM120/SM89 respectively, AVX2_256,
thread-MPI/OpenMP, one GPU, 12 requested CPUs, 24 GiB RAM and 20 GiB network scratch.
S2 chooses GPU/region and the corresponding image; the application checks the
actual attempt against its immutable profile. RTX is preferred; L40S is allowed.

The isolated Terraform root creates five resources. Its final plan reports
**no changes**. The route is Accepted/ResolvedRefs; the existing S2 API remains
reachable. An initial Kubernetes-provider default incorrectly set the container
to `runAsNonRoot=false`; it was repaired on the owned pilot Deployment and in
Terraform. Later image/config rollouts used Terraform successfully. A complete
destroy/recreate of the fixed root was **not** performed in this acceptance.

## Unchanged Scientific AI release: initial execution cohorts

These two cohorts used the same application/worker release, but **not a frozen
Serverless release**: A ran before the S2 upgrade; B/recovery ran on 0.11.12.
Additional post-upgrade checks are recorded separately below. Do not treat this
table as two unchanged-full-stack customer-release cohorts.

All use a **new 4,096-particle LJ argon fixture**, seed 1741, 2-fs timestep, NVE,
initial velocity temperature 120 K, 1-nm cutoffs and no PME. This is a runtime and
recovery qualification fixture, **not a protein/PME benchmark or a capacity SLO**.

| Cohort / path | Public operation ID | Actual GPU / region | Steps / artifacts | Native ns/day |
| --- | --- | --- | --- | --- |
| A / REST | `bb1bd6b8-01cb-50ad-847b-1ed08302d5bd` | L40S / eu-north1 | 100,000 / 13 | 9,210.192 |
| A / MCP | `42d4144a-aef9-500f-a7d8-568c81cb1680` | RTX / us-central1 | 100,000 / 13 | 7,160.130 |
| B / REST | `3c8b7168-1b3a-5e6d-af28-bb93cfe5491f` | RTX / us-central1 | 100,000 / 13 | 7,568.482 |
| B / MCP | `ec90358e-83de-5af0-a3dd-6c61b360a9c9` | RTX / us-central1 | 100,000 / 13 | 6,268.171 |

Each finished in one attempt. Every output was downloaded through the protected
API and checked for exact length/SHA-256. Native preparation occurred once, and
the final native checkpoint contains exactly the requested steps. Real
`gmx check` parsed every XTC: 101 frames, 4,096 atoms, 0–200 ps, 2-ps spacing.
MCP submission, status, result and resume were exercised, not merely tool discovery.
Binary artifact bytes use the authenticated HTTP download path returned by MCP.

When both RTX slots were occupied, cohort A's REST request selected L40S without
changing the submitted request. It selected L40S at admission; this does **not**
prove that a running job automatically changes class after spot loss.

| Run | Accepted → worker start | Worker lifetime | Accepted → durable S2 completion |
| --- | ---: | ---: | ---: |
| A REST / L40S | 67 s | 32 s | 112 s |
| A MCP / RTX | 69 s | 55 s | 139 s |
| B REST / RTX | 132 s | 55 s | 203 s |
| B MCP / RTX | 83 s | 55 s | 153 s |

Times use S2's creation/attempt/final timestamps, not client polling delay.
Native simulation itself took 2.27–3.43 s. These end-to-end timings include
staging, conservative object read-back verification, checkpoint acknowledgement
and finalization. The slowest warm start spent approximately 98 s attaching a
network volume; its image was already cached. A new r5 worker image pull took
7.512 s in an earlier observed event. Initial scale-from-zero RTX startup before
the r5 cohort took about 7.5 minutes including infrastructure and storage, not
just the model container. No fast-start target has been qualified yet.

### Final two cohorts on Serverless API 0.11.13

After noticing the independent fleet upgrade, both interfaces were repeated in
two new cohorts against the same S2 API image
`sha256:fc8ee5b1ee498eead298cc893ce485b4249bd02eae4736a63560fc487204423d`
and unchanged r5 application images. All four ran on RTX/us-central1, completed
100,000 steps in one attempt, returned 13 hash-verified files and passed real
GROMACS trajectory parsing (101 continuous frames each).

| Cohort / path | Public operation ID | Start delay | End-to-end |
| --- | --- | ---: | ---: |
| A / REST | `5c37f482-345a-5600-ba53-7cff9b3bb8da` | 100 s | 170 s |
| A / MCP | `6fd3c829-e771-59a1-96e7-ba7820edbffd` | 70 s | 141 s |
| B / REST | `b05692b6-2404-5ca6-b612-785fd9101fd8` | 56 s | 126 s |
| B / MCP | `5cae8ad6-c32a-5df7-bb9f-388ba3e15db6` | 72 s | 142 s |

All three upstream contract probes, concurrent replay/conflict checks and
terminal storage denial also pass after that upgrade. Replaying the existing
RTX-to-L40S continuation via MCP still returns its original successful ID.
The abrupt cross-GPU drill itself ran on 0.11.12; it is not relabelled as a new
0.11.13 fault injection. Existing handles/results survived the S2 API rollouts.

## Abrupt loss and cross-GPU recovery

1. New 2,000,000-step REST operation
   `bc273a25-60c1-5d2e-ab14-76fdcc3ea682` → `op-0c1b75b8659e5135` ran on RTX,
   us-central1, pod `op-0c1b75b8659e5135-nxjfz`, epoch 1.
2. At **15:35:46 UTC**, the latest acknowledged bucket checkpoint was native step
   **1,281,200**, ref
   `aace7ffd4c97814a07621e43f57a8f6a49c45985033d2b62c446a015d7e827b6`.
3. The driver force-deleted only that verified synthetic pod. It then cancelled
   the original via REST to prevent an RTX retry racing the explicit L40S test.
   The exact original PVC `op-0c1b75b8659e5135-work` was deleted at 15:35:53 UTC.
   Read-only checks confirmed both original pod and PVC absent.
4. REST resume with `gpu_class=l40s` created
   `f267dd4c-8498-546a-aabe-f214bb7218a2` → `op-0c1b75b8659e5135-r-4f374b9a`.
   Replaying the same continuation through MCP returned that same ID.
5. The new L40S pod `op-0c1b75b8659e5135-r-4f374b9a-l9f5p` ran in **eu-north1**,
   epoch 2, with the SM89 digest and a **new PVC**. Native command segment 3 used
   `-cpi fs2-md.cpt`; the original `grompp` command was not repeated.
6. Worker start 15:40:00, end 15:40:42; S2 durable success 15:40:55 UTC. Admission
   to worker start was 58 s; total continuation was 113 s, including 14.49 s of
   native MD. The manual gap before submitting resume is not counted as automatic
   failover latency. Final native step **2,000,000**, **22 verified output files**.

Real GROMACS independently checked all recovered trajectory segments:

| Segment | Frames | First / last ps | Interval |
| --- | ---: | --- | --- |
| RTX part 1 | 648 | 0 / 1,294 | 2 ps |
| RTX part 2 | 634 | 1,296 / 2,562 | 2 ps |
| L40S part 3 | 719 | 2,564 / 4,000 | 2 ps |

That is **2,001 continuous frames**, no missing/duplicated output interval.
Durable step ranges are known; work executed after the last acknowledged
checkpoint before the forced loss is **not measured** and is not called zero.
This was abrupt pod/process loss followed by API resume, not an actual provider
spot-eviction or automatic post-placement cross-class retry.

## Completed-result replay and authorization

- Completed RTX operation `42d4144a-aef9-500f-a7d8-568c81cb1680` was resumed through
  MCP on L40S as `d1c87d97-5715-583d-b551-2bec48f4612a`. It returned all 13 outputs
  with an **identical native result and zero additional engine commands**.
  Worker lifetime 11 s; full continuation 91 s. This proves result-manifest
  replay, not recovery from every possible pre-manifest export failure.
- Anonymous REST and MCP: 401. Other internal principal requesting an operation
  or artifact: 404. Six concurrent identical submissions returned one operation;
  conflicting intent with the same key returned 409. REST↔MCP replay preserved
  IDs, and API rollouts did not lose existing operation handles.
- A completed logical job's storage capability could no longer obtain a new
  object URL: 403. Previously issued signed URLs have a **60-second** residual TTL.
  GPU pods received no tenant-wide S3 key. Native data stayed in the dedicated
  internal run prefix, not a shared model cache.

## Failures found and repaired

Keep these in the evidence; the first attempts were not clean passes:

1. **Conditional S3 create:** Nebius returns `KeyAlreadyExists`, not just 412 /
   `PreconditionFailed`. This caused an initial upload/replay error. The adapter
   now handles the actual provider code; concurrent live replays and four
   provider-code regression cases pass.
2. **Transient object read timeout:** the two initial r2 RTX jobs each retried
   once under S2 after a timeout. r5 adds bounded retries for immutable transfers
   and rewinds/reopens streams, without retrying simulation/submission. All four
   initial and all four post-upgrade r5 cohort jobs completed in one attempt. Authorization/corruption are
   not retried. This is not proof that future storage/network failures cannot occur.
3. **Terraform non-root default:** repaired as described above.
4. **Acceptance-driver resume body:** it initially put the idempotency key in the
   REST body as well as the header; the API correctly refused it with 422. The
   driver now uses the header only. No scientific work was admitted by that call.

94 application tests, 75 existing GROMACS worker tests, lint, wheel build,
Terraform validation and final no-drift plan pass. The three S2 contract probes
pass. Earlier CPU-only recovery evidence remains separately labelled.

## Remaining gates — do not describe these as finished

- **Automatic loss recovery across GPU classes:** S2 automatically retries a
  failed worker in its class and can fall back for *unplaced* work. Automatic
  movement of an already-placed failed/evicted run to another GPU/region remains
  a Serverless scheduling/recovery gate. Do not add a competing app scheduler.
- **Cryptographic attempt identity for object signing:** this broker checks a
  logical-job capability plus a supplied attempt ID against live S2 state. It
  does not cryptographically bind the requester to that pod. A stale holder
  who knows the replacement ID is not excluded by this check alone. Use an
  approved pod-bound identity/audience and verify signed pod claims before
  customer/PHI use; a wrong-ID negative is not an attestation test. Cross-container
  `/proc` isolation is also unqualified. S2 checkpoint publication itself is
  independently authenticated and epoch-fenced by its uploader.
- **External customer storage / protected data:** AWS role/trust is a placeholder;
  real cross-account IAM, token renewal/revocation, another tenant, confidential
  scratch/log cleanup and BAA/service-scope requirements remain unqualified. The
  internal demo bucket is not evidence of external-AWS or HIPAA readiness.
- **Failure/scale qualification:** deny final output storage, lose the final
  acknowledgement, partition a replaced worker, run genuinely large trajectories
  and sustained multi-user load. Unit tests are not live coverage for these.
- **Full old-platform compatibility:** result envelope is explicitly v2; artifact
  manifests are v1. Batch fan-out, MPI, production keys/users, admin/usage/billing,
  continuation-budget changes and extensions are not migrated. Legacy resume
  `job_id`/`performance_mode` are currently accepted but not applied; reject or
  implement them before migrating clients. Existing production keeps these features.
- **Release:** repository-wide CI, clean-root Terraform recreate acceptance and
  explicit main/customer integration approval still apply. Stay on the migration
  branch; neither a successful pilot nor this report authorizes a cutover.

## Reproduction, retention and cleanup

Run `pilot.py` with the package's `uv` environment. It reads existing internal
keys without printing them. Private configuration, Terraform state, raw receipts,
native files and trajectory check receipts are in
`/home/tux/secure-handoff/scientific-ai-s2-pilot-20261011` (0700).
The bucket prefix is `runs/scientific-ai-s2-gromacs-20261011`; no new bucket/user
was created. Safe summary: [`live-pilot.json`](live-pilot.json).

```sh
# From k8s-inference/components/scientific-ai; no key values on command lines.
uv run python ../../acceptance/serverless2-gromacs-20261011/pilot.py status \
  --private-dir /home/tux/secure-handoff/scientific-ai-s2-pilot-20261011 \
  --name recovery-r5-resumed --path mcp

uv run python ../../acceptance/serverless2-gromacs-20261011/pilot.py trajectories \
  --private-dir /home/tux/secure-handoff/scientific-ai-s2-pilot-20261011 \
  --name recovery-r5-resumed \
  --engine-image scientific-ai/gromacs-engine:2026.4-sm89-pilot
```

`prepare` defaults to r5; use `--tag pilot-20261011-r5 --api-tag pilot-20261011-r5`
for explicit reproduction. `submit` requires a fresh name for new
work; reuse the original key/name for a retry. `interrupt` refuses anything but
an owned synthetic RTX run with a strictly mid-simulation acknowledged checkpoint.
It deletes only the verified run's pod/PVC, never a node. Do not use it on a
customer run or repeatedly after the fault has already completed.

Owner: Scientific AI migration. Retain the deployed pilot API/model and its
synthetic results for Rene's evaluation; review retention on **18 October 2026**.
This date is a review date, not an installed deletion timer. No GPU test is left
intentionally running; normal S2 autoscaling can return the zero-floor RTX/L40S
pools to zero. Other lanes' floors/capacity remain untouched. The old faulted
scratch PVC was deleted; checkpoint data remains recoverable in object storage.
At roughly 15:57 UTC the observed test-node time was approximately **$1.9** at
the fleet's recorded spot rates (two RTX nodes at $1.09/GPU-h, one L40S at
$0.85/GPU-h). This excludes pre-registration boot time, disk/network/API costs
and subsequent idle retention; it is not an invoice or a cost-per-request estimate.
