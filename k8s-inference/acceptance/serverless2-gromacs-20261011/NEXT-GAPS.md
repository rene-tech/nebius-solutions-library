# Next integration gates — 12 October 2026

Scope: shared `s2pr2` fleet only. Keep Scientific AI on
`scientific-ai-serverless2-migration` and Serverless changes on its existing
unmerged PR branch. Do not touch the customer cluster, customer keys/data, or
shared GPU pool bounds. Existing r10 evidence remains historical evidence.

## Implementation and acceptance plan

1. Add opt-in, bounded, native-checkpoint recovery to Serverless's existing
   reconciliation loop. Detect confirmed infrastructure loss, not application
   errors or an unavailable control API. Keep one public operation handle,
   pinned per-GPU images, private-input delivery, original deadline and
   cancellation semantics. No application-side scheduler.
2. Exercise denied final export, lost final acknowledgement, stale-worker
   access and export-only recovery. Require durable results before success;
   retain recoverable data and prove no unnecessary simulation rerun.
3. Run a public protein/PME fixture through REST and MCP on the RTX and L40S
   runtime images. Record the immutable input, protocol, completed native steps,
   trajectory checks, ns/day and startup/recovery time. No customer inputs.
4. Qualify external storage only with an actual approved provider bucket and
   IAM trust configuration. Stubs and the internal Nebius test bucket are not
   evidence of AWS federation or PHI readiness. Record any missing owner input.
5. Deploy exact validated images with narrowly scoped Terraform plans, repeat
   unchanged-release cohorts, retain failure evidence and clean only owned
   temporary compute/scratch. Publish commits and update this receipt.

Status: isolated implementation and live acceptance in progress. The first
unchanged-release cohort passed native execution and automatic recovery; the
second is still running. This is not a customer cutover or full migration verdict.

## Application progress

- The API passes operator-owned bounded recovery settings to S2 and checks
  discovery before admission. Legacy defaults remain unchanged. Stale-pod
  object access and successor result handles have focused regressions.
- Local checks: 142 application tests pass (one explicitly gated native test
  skipped in that invocation), 75 reused-worker regressions pass, and the
  explicitly enabled real-engine CPU recovery test separately passes.
- The first native-test invocation correctly rejected a mutable image tag;
  rerun used the existing r6 SM89 digest and passed. This is test invocation
  evidence, not a new GPU qualification.
- `pilot.py submit --benchmem-tpr <path>` accepts only the SHA-256-pinned
  [MPINAT benchMEM](https://www.mpinat.mpg.de/grubmueller/bench) input (CC-BY-4.0;
  attribution in `protein_fixture.py`). 81,743 atoms, PME, 2 fs, all-bond
  constraints with CPU update. `convert-tpr` changes only finite `nsteps`;
  local `gmx check -s1/-s2` confirmed that is the sole input difference.
  Original trajectory/energy output is disabled: do not claim trajectory or
  thermodynamic validation from this fixture. Validate final coordinates,
  native checkpoints, completed steps and immutable returned artifacts.
- Existing synthetic XTC continuity evidence remains a separate gate, not a
  substitute for the protein/PME workload. A successful protein job is not
  proof of ensemble convergence or scientific suitability of Berendsen coupling.

## Isolated rollout (not a qualification verdict)

The pilot Terraform plan changed only its configuration Secret and Deployment
(zero additions/deletions). Private storage bindings, signing key and workload
identity are unchanged. Public health returned 200 before and after rollout.

- Application source: `a6489c8c4`; API r12 digest
  `sha256:bb99756932f449b8e1af7e23d9e54abaab9b71e8d76b2e513081decfc73bd294`.
- Worker source: `7bf213003`; r7 SM89 digest
  `sha256:ae08add31eab27ca823d37023f5e55586538d45dd35b2bf65470a9fdf1bbea0a`;
  SM120 digest
  `sha256:3ee76513fa6549ac900c3762db740bff72d4d8d004fb94d88f4b72a8d6a0d5fc`.
- The owned public-protein job `op-77676101178fdab9` triggered RTX scale-up
  from zero. Node created `2026-10-12T14:23:48Z`; main container started
  `14:28:42Z`, after GPU driver/device-plugin initialization. This includes
  cold-node provisioning and is not comparable to warm-node startup alone.
- This first job overlaps S2 upgrades: provisional runtime evidence only,
  not an unchanged-release acceptance cohort. No fault was injected while
  the superseded-worker fence was being corrected.

### Provisional benchMEM result

`r12-benchmem-rest-1` completed successfully: 200,000 native steps (400 ps),
four MD segments, one preparation command, 19 returned files with matching
size/SHA-256, and 81,743 finite final coordinates. REST submission and six
concurrent idempotent replays retained one operation; MCP replay/result worked.
Anonymous calls returned 401, another internal principal's operation/artifact
requests returned 404, and changed input with the same key returned 409.

Measured timestamps (UTC, actual workload metadata):

| Boundary | Time / duration |
| --- | --- |
| Operation created | 2026-10-12 14:21:10 |
| Main container started | 14:28:42; 452 s after admission |
| Main container ended | 14:34:35; 353 s occupied main lifetime |
| Operation completed | 14:34:50; 820 s after admission |
| Native command wall time | 225.700 s |
| Native segment performance | 139.576, 136.694, 184.085, 199.740 ns/day |

This is a six-CPU, single RTX PRO 6000 Blackwell Server Edition run, driver
580.173.02, with native SM120 image above. The native segment rates are not
end-to-end throughput: provisioning, staging, checkpoint transfer and
finalization are separate costs. The segments include PME autotuning and
must not be compared as identically warmed steady-state measurements.
No thermodynamic convergence, GPU snapshot, automatic recovery or AWS
qualification follows from this result.

Receipts remain under the private directory documented in README.md, prefix
`r12-benchmem-rest-1`: request, timeline, status, native result, downloaded
files, native validation, boundary and MCP replay receipts. No credentials
or signed URLs are copied into this report.

## Unchanged-release cohorts — S2 0.11.23 / API r12 / workers r7

S2 API source `89e38ea89`, image
`sha256:ba8a7460e0781a08ebbb9a32e781175d3e1d2bf457ab3426d9aa5e0d5a16a701`,
was ready at 14:39:05 UTC. No S2 or application rollout occurred during these
cohorts. The native worker images are the r7 digests above. All operations use
the internal `sciai` test identity, public benchMEM data and existing test
storage. GPU pools retain their existing zero floor and maximum of two nodes.

| Receipt prefix | Submission path | Placement | Result so far |
| --- | --- | --- | --- |
| `r12-c1-auto-protein` | REST | RTX, then automatic L40S recovery | 500,000 steps; 49 files verified; complete |
| `r12-c1-mcp-protein` | MCP | RTX | 200,000 steps; 19 files verified; complete |
| `r12-c2-auto-protein` | MCP | RTX, then automatic L40S recovery | Running on successor; artifacts not yet qualified |
| `r12-c2-rest-protein` | REST | Initial RTX-capacity fallback to L40S | 200,000 steps; 22 files verified; complete after observer timeout |

Every completed case verifies each returned file's size/SHA-256, requested
native checkpoint step and 81,743 finite final coordinates. Preparation runs
exactly once. The first recovery returns 14 MD segment receipts and 819.823 s
of recorded native command time across the acknowledged history; this excludes
unacknowledged work lost on interruption and is not total GPU occupancy.
The first MCP case records 215.494 s of native commands, 320 s main-container
lifetime and 416 s admission-to-durable completion. Its segment rates are
192.239, 186.052, 150.366 and 120.673 ns/day, not steady-state tuning results.
The second REST case records 277.130 s of native commands, 334 s main-container
lifetime and 1,261 s from original admission (14:44:29) to durable completion
(15:05:30), including 914 s before main started. Its native segment rates are
105.868, 165.101, 94.839, 156.277 and 152.817 ns/day on L40S.

Both recovery drills deactivate only their exact owned Kueue Workload after
a native checkpoint is durably acknowledged. There is no node deletion,
customer job mutation, manual `:resume`, or application scheduling loop.

| Recovery boundary (UTC) | Cohort 1 | Cohort 2 |
| --- | --- | --- |
| Original submission | 14:40:08 | 14:44:07 |
| Injected infrastructure loss | 14:43:02.405 | 14:47:39.959 |
| Acknowledged native checkpoint | Step 52,300 | Step 52,600 |
| Successor created | 14:43:15 | 14:47:52 |
| Successor main started | 14:44:30 | 15:01:09 |
| Successor observed running | 14:44:40.159 | 15:01:21.599 |
| Durable completion | 14:59:22 | Pending |

Both preserve the original public operation UUID, switch to the pinned SM89
image and increment epoch 1 to 2. The original pod's real token, even paired
with the replacement attempt ID, returns 403; the replacement token returns
200. Missing and forged identities also return 403. The root's remaining
deadline is inherited, not reset. These are native GROMACS checkpoints, **not
CUDA/GPU snapshots**.
After cohort 1 completed, requesting a new object URL using its previously
accepted real pod identity also returned 403. Existing signed URLs retain
their documented maximum 60-second lifetime; immediate invalidation of an
already issued URL is not claimed.

Cohort 2 deliberately retains observed capacity waiting: the second L40S
spot node was requested at 14:46:25, registered at 14:57:33 and reported Ready
at 14:57:52. Its first tested main container started at 15:01:09 after GPU
bootstrap. Recovery took about 822 s to be observed running, versus 98 s
in cohort 1; neither is an isolated weight-load measurement. No pool limit
was changed to hide that delay.

The cohort-2 REST observer reached its 1,200-second polling bound while the
same operation was still running after the capacity wait. This is a failed
end-to-end observation-time bound, retained as evidence, not a failed native
simulation or a passing latency result. Observation was restarted against
the **same** operation; no job was resubmitted and no platform deadline,
quota or GPU-pool limit was increased.

### Found during acceptance: incomplete public attempt history

On S2 0.11.23, following a recovered handle returns the successor's
`created_at` and `attempts` only. Thus the public result's `submitted_at`
is 14:43:15 instead of the original 14:40:08 in cohort 1, and original RTX
allocation time is absent from that response. The full private acceptance
timeline and S2 records retain the evidence; this is a projection/accounting
gap, not loss of the native checkpoint or completed artifacts.

Claude is preparing a source-only chain-history correction: preserve effective
identity/current epoch for authorization, expose the root submission time and
all attempts, and accumulate measured consumption without treating unknown
values as zero. Successor creation must not be called completed recovery;
queueing and actual ready/main-start timestamps stay distinct. The correction
is not deployed and does not count as part of this cohort's evidence.

## Remaining delivery contract

`finish_delivery()` is tested locally but is not exposed as a public,
CPU-only retry. Today, denied final delivery exits 75 and S2 reports `FAILED`
with scratch retained. S2 `:export` accepts `EXPORT_FAILED`/`FINALIZING` and
only reposts an existing final record in application-I/O mode; it cannot
upload Scientific AI-owned files after storage denied their upload. Do not
advertise this path as implemented or count local doubles as a live fault test.
An app-owned upload retry needs coordinated S2 attempt identity and retained
scratch access, without a second scheduler or scientific recomputation.

Claude proposed a model-declared finalizer on the existing `:export` operation.
That is the preferred integration direction, not an implemented feature. The
remaining contract must cover:

- CPU-only execution in the pinned model image, private binding and retained
  original scratch; no request-supplied command and no customer storage logic
  in S2. CPU-only attempts must also record **zero GPU allocation** durably.
- One idempotent active finalizer per root. The original handle and cancellation
  must resolve its active pod/epoch, not only recognize `exported_by` after
  completion; otherwise the storage broker will reject the replacement.
- An application-owned, validated delivery marker selects the completed
  workspace. Do not guess the old pod's directory, confuse it with the new
  finalizer workspace, or allow a failed scientific computation to become a
  successful result merely by retrying export.
- Keep CPU-only finalization distinct from GPU execution in the attempt contract
  and runtime authorization. `Attempt.gpu_class` currently assumes a GPU, so
  simply reusing `finish_delivery()` is insufficient.
- Prove denied final upload and lost acknowledgement on real object storage,
  with unchanged scientific command counts, zero finalizer GPU allocation,
  old-pod denial, current-pod access, cancellation, bounded retry and cleanup.

Source inspection also found S2's current CPU-only `:export` retaining the
original uploader GPU-count environment. Claude has fixed that accounting issue
in an undeployed candidate; the recovery cohorts remain on **0.11.23**. Do not
conflate the pending accounting candidate or finalizer proposal with the live
release.

Existing customer deployment and main remain unchanged; Claude owns shared
Serverless release coordination. AWS qualification still needs an actual
approved test bucket and IAM role, not the fleet's placeholder role.
