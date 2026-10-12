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

Rollout and GPU/failure receipts are still pending. Existing customer deployment
and main remain unchanged; Claude owns shared Serverless release coordination.
