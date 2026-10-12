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

Status: implementation in progress; no new acceptance claims yet.

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

## Remaining delivery contract

`finish_delivery()` is tested locally but is not exposed as a public,
CPU-only retry. Today, denied final delivery exits 75 and S2 reports `FAILED`
with scratch retained. S2 `:export` accepts `EXPORT_FAILED`/`FINALIZING` and
only reposts an existing final record in application-I/O mode; it cannot
upload Scientific AI-owned files after storage denied their upload. Do not
advertise this path as implemented or count local doubles as a live fault test.
An app-owned upload retry needs coordinated S2 attempt identity and retained
scratch access, without a second scheduler or scientific recomputation.

Existing customer deployment and main remain unchanged; Claude owns shared
Serverless release coordination. AWS qualification still needs an actual
approved test bucket and IAM role, not the fleet's placeholder role.
