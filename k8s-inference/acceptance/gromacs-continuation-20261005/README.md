# Seven-day GROMACS jobs and durable continuation — 2026-10-05

Scope: the six-hour timeout and recovery of Lynx operation
`aa502153-3c75-422c-8040-82461fdbfcaa`. This is not a claim that every platform
App, large MPI shape, or seven-day continuous run has been qualified.

## Released implementation

- Default and maximum native GROMACS/GROMACS-MPI job budget: 604800 seconds.
  Kubernetes permits 606600 seconds including staging/final export grace.
- A local execution-budget timeout stops the engine cleanly and allows the
  companion to acknowledge the final durable checkpoint. It reports
  `WORKFLOW_TIME_LIMIT_EXCEEDED`, not generic Kubernetes backoff or success.
- Public REST checkpoint discovery and explicit continuation, with MCP parity:
  `get_scientific_checkpoints` and `resume_gromacs_workflow`.
- A continuation is a new, normally admitted operation. Completed preparation
  is skipped. Original TPR, native checkpoint and earlier trajectory parts are
  retained. It is not an unlimited automatic retry loop.
- Native files stream from object storage into the replacement worker. No
  trajectory archive is downloaded/repacked on the API server.

See [the continuation API and storage contract](../../models/molecular-dynamics/gromacs/CONTINUATION.md).

The engine still uses a private normal filesystem while computing. At closed
segment boundaries, files and the checkpoint manifest are uploaded to durable
storage and, by default, the customer bucket. S3 is not treated as a POSIX disk.
An unfinished segment can be lost on sudden node failure; previously committed
segments must survive independently of that node.

## Exact production identities

Regional repository prefix:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/`.

| Component | Immutable image digest |
| --- | --- |
| API and scientific tools (r4, deployed) | `sha256:f02e712a5dfb834a054d653ec25062c9946f94e32560abc404e9f989616ce5e5` |
| GROMACS worker | `sha256:f633539e6ea28bcacd3b4eaee44235ace4c33a55e16fa3d50db047b1f37831b6` |
| External-MPI worker | `sha256:938cc6124eaf7840965bebd24b9ae04014e98651a4260f39266a29c8131c202b` |

The engine base images are unchanged; only the platform Python wrappers changed.
API source: `23cacc70524a7994667873bbb881a56f6d5c8c23`.
Execution ConfigMap: `fs2-r927c465c6d-scientific-execution-d524262ff902`.
Scheduling/pools, Gateway, Services, TLS, keys and customer limits were unchanged.

Compatible readers were deployed before activating plans with the longer
deadline. **Do not roll back to readers older than this compatibility change**:
stored completed and active plans now legitimately contain the longer deadline.
Draining alone does not make an old reader compatible.

## Evidence and failures retained

Private evidence root:
`/home/tux/secure-handoff/fs2-gromacs-continuation-20261005`.

- `native-h100-r2`, `native-l40s-r2`: exact wrapper, repeated native simulations,
  finite coordinates and native energy/output checks passed. Task-owned probe
  Pods were deleted. Initial probes failed because the qualification harness
  assumed `gmx` was on PATH; those receipts are retained and the harness now
  resolves the real image-configured binary.
- `native-mpi-h100`: one-GPU external MPI, native checkpoint continuation,
  rank binding and repeated completion passed; probe Pod deleted. This does
  not qualify 8/16-GPU distributed recovery.
- `acceptance/rest-1`: source `c6f18491-c0a8-4ed3-a9ad-bfdfcd1fa7cf`, continued
  as `bd4dbf9a-7a7c-4f58-9b28-4d65b242e258` from step 22400 to 60000.
- `acceptance/mcp-1`: source `0a43fff7-82e9-4143-b652-2be8b9b083a7`, continued
  as `f78d0261-3fed-408b-8755-c9d135b5350d` from step 22100 to 60000.
  Both preserved eight earlier native files/TPRs and exported checkpoints to
  the existing system bucket. These use the internal system/qa key, never a
  customer key.
- The first backend candidate correctly resumed both simulations but returned
  409 on replay: write-once continuation metadata had already been finalized.
  The r2 release reuses that verified artifact instead of overwriting it.
  Retained original evidence includes `acceptance/rest-1/resume-error.json`.
  Replay of both existing operations passed after the fix. A regression test
  now exercises the real artifact/upload services, not just a permissive fake.
- Fresh unchanged-release REST/MCP repeats passed in `acceptance/rest-2`
  (22400 → 60000) and `mcp-2` (22000 → 60000). The public single-GPU external
  MPI continuation passed in `mpi-1` (34000 → 200000), preserving eight native
  files and using MCP for continuation. The first MPI
  submission was rejected with 429 by ordinary internal QA admission before
  any simulation; no quota was changed to bypass that limit.
- `feature-release-r2/reader-verification/verification.json`: all three ready
  API readers matched the exact r2 image; authenticated public discovery 200.
  Public website returned 200 after each release; no public routing changed.
- System/qa checkpoint discovery against the Lynx source returned 404, as
  required by tenant isolation.

### Large-file recovery defects and availability incident

The first operator-authorized Lynx continuation was
`7afecf8e-e59a-4717-8868-58d4fdcceff9`, accepted at 13:27:48 UTC. It did **not**
run GROMACS and must not be described as recovered work:

1. The new admission writer accepted 194 unique input files, but the existing
   durable decoder still allowed only 64 materializations/consumed inputs.
   The pending admission then failed repeatedly in shared worker loops and
   made API readers unhealthy. Public API 503 was also observed by the internal
   MPI qualification test. The website continued to return 200.
2. Reader r3 (`843251b9b8bb19564047e184b4d5c7bd7131987ac3f5dc302eb8af33047c11c6`)
   restored API readiness around 13:36 UTC and decoded the pending plan. Both
   arrays now use the existing 10000-entry manifest limit; the durable outbox
   still enforces its 4 MiB serialized payload bound. Regression cases cover
   65, 305 and 1000 files. The source customer's artifacts were not changed.
3. That continuation then failed in input staging at 13:36:50 UTC: the legacy
   artifact capability copied all 194 file records into an oversized HTTP
   authorization header. No simulation steps ran. Internal `many-files-1`
   reproduced the same failure with over 70 files and retained the evidence.
4. Release r4 uses a compact, signed digest of the ordered immutable input
   identities for large GROMACS capabilities. The API reconstructs those
   identities from durable admitted state and verifies the digest and original
   tenant/attempt fences. No larger header limit or access widening is used.
   Legacy small v1 tokens remain accepted for already-running workloads.
5. Admission now runs the actual durable decoder before the transaction commits.
   A writer/reader contract mismatch is rejected before an operation/outbox row
   is saved; an injected regression confirms that no durable admission remains.

Local r4 regression: **179 tests passed**, including the compact capability,
durable plan roundtrip, authorization binding changes, REST/MCP resume service,
checkpoint publication, execution shapes and existing scientific handoff tests.
Ruff and the three changed backend modules' mypy checks passed. This does not
replace the real large-file public-path acceptance or qualify distributed MPI.

All deployment patches preserve unrelated App maps, scheduling, keys and
customer limits. Rollback must retain a reader that understands both the longer
deadline and many-file plans. Once compact tokens are issued, retain support for
v2 while their attempts run.

### r4 public-path acceptance

- `feature-release-r4/reader-verification-ready/verification.json` confirms
  three ready r4 API replicas and public authenticated model discovery 200.
  The first verification intentionally rejected a still-transitioning rollout;
  it was repeated after all old readers exited, not bypassed.
- `many-files-mpi-mcp-r4`: source
  `60eb1b9d-80ba-4de4-a03e-b7aeb7d08d91` → continuation
  `3922a0f0-ffd6-4784-b686-c7a23f95c377`, **passed**. It resumed exactly at
  step 23900 and completed 200000 steps, preserving 76 earlier native/input
  files. Native validation and same-key replay passed; the final checkpoint
  was exported to the existing system bucket. One L40S GPU, external MPI.
- `many-files-rest-r4` deliberately had 240 additional small input files.
  Its 60-second source budget expired while exporting the first preparation
  checkpoint, before any native simulation checkpoint existed. This is retained
  as an unsuccessful test, not counted as a successful resume or hidden.
  `verify_resume.py --source-seconds 120` allows the corrected bounded test to
  reach real simulation; the deployment was not changed for this repeat.
- `many-files-rest-r4-b`: source
  `5486a6cc-424a-4f8f-a21a-1117a5d15efd`, continuation
  `31e35dec-0c4e-4de6-a577-14fb1b8b4833`, **passed**. It resumed exactly at
  step 16900 and completed 60000 steps, preserving 246 earlier native/input
  files. Native validation, unchanged TPR/old-part identities, same-key replay,
  and customer-bucket export passed.

### Qualification limits

No seven-day soak or 8/16-GPU recovery was run. Native live many-file acceptance
and unit metadata tests are separate: a 1000-entry codec/capability unit pass
does not prove that a 1000-file Kubernetes launch fits all envelope limits.
Existing output/file/workspace bounds and the 4 MiB admission bound remain.
The current renderer also carries invocation metadata in an environment value;
very large continuation file lists need a file-backed/remote descriptor before
they can be claimed supported (Linux per-environment-string and Kubernetes
object limits still apply). Thousands-of-segments continuation is **not**
qualified by this incident fix. Closed original data remains in object storage
even if a later continuation exceeds an admission/launch bound.

The bounded tests deliberately use a 60- or 120-second source budget and a seven-day
resume budget, but finish a finite simulation in minutes. **No seven-day soak
test is claimed.** They establish timeout publication, remote restore, original
step continuity, output preservation, idempotency, and endpoint behavior.

## Authorized Lynx recovery

The original failed operation remains immutable. After the public r4 tests
passed, the operator-authorized continuation was accepted at
`2026-10-05T14:10:26.161274Z` as
`a42479f9-5ee0-4ed4-869b-0a094357403f`.

- Current owner policy was resolved from the original operation; no customer
  API key was used for QA or exposed. No other active Lynx MD operation existed
  before recovery. The same recovery key is replay-safe.
- Native budget: 604800 seconds; actual Kubernetes Job deadline: 606600.
- Pod: `fs2-workflow-mas1-20e-a1-7d7046f29109-74jtt`, one L40S GPU.
  Input materialization completed, and the scientific container started at
  14:11:05 UTC. Its first native log confirms continuation at step **13963440**.
- The normal five-minute segment interval and original 4 GiB output allowance
  were retained; no physics, quota, scheduling, or customer credential changes.
- `lynx-recovery-r4-submitted.json` is submission evidence only.
  `verify_lynx_recovery.py` performs read-only verification of a newly committed
  checkpoint, native start step, forward progress, old part/TPR hashes and
  export to `fs2-lynx-c327dcc386444425`.
- **Verified at 14:18:34 UTC:** the new run resumed at 13963440 and durably
  advanced to **14163520**. All **70 original trajectory parts** and **211
  native output/TPR files** retained their original byte count and SHA-256.
  Source/continuation lineage was verified. Receipt:
  `lynx-recovery-r4-verified.json`.
- New checkpoint artifact: `e136eda1-5e2d-4924-8932-515db398aae8`, SHA-256
  `82e940f7f38ae437a6aff4f8e0b6252c0b81c68285a79e7f3b25a85747d579d4`.
  Customer manifest:
  `s3://fs2-lynx-c327dcc386444425/runs/mas1-20e-production-r3/a42479f9-5ee0-4ed4-869b-0a094357403f/mas1-20e/attempt-001/checkpoint-00000001.json`.
  The job is deliberately left running. This is recovered forward progress,
  not completion of its 1 µs simulation.
- Independent customer-bucket GET also returned **200**: manifest 69341 bytes,
  native checkpoint 4453996 bytes, checkpoint content SHA-256 verified against
  that manifest. Receipt: `lynx-customer-bucket-verified.json`. This used the
  existing attempt-bound storage broker, not a customer inference API key.
  The platform-artifact account correctly could not HEAD the separate customer
  bucket (403); it is not the credential to use for this check. The first
  scoped GET verified content but the one-off probe then called a nonexistent
  client cleanup method; the corrected read-only probe passed with exit zero.

Retention code was also inspected: the normal PostgreSQL operation purge uses
time since completion, excludes active runs, and preserves operations with
scientific batch/artifact references. Explicit continuation reads the durable
admitted workspace request, not the shorter-lived generic HTTP request payload.

## Persistence / next release integration

Implementation lives in the isolated worktree
`/home/tux/worktrees/fs2-gromacs-continuation-20261005`, based on
`a0dd1f9aad5c9e6302d7d45bbe91c94f5909dd5a`.
No parent worktree or in-progress benchmark source was overwritten. Integrate
the scoped commits before another API build so a later release cannot silently
drop the fix. Exact deployment patches, rollback constraints and the narrow Helm
overlay are retained under `feature-release-r4`; existing active jobs retained
their frozen execution plans.

The original Lynx checkpoint is generation 71, step 13963440 (27.92688 ns), with
70 completed trajectory parts. Its target is 500000000 steps (1 microsecond).
At the previously measured approximately 116 ns/day this is an approximately
8.6-day total simulation, so one seven-day continuation may still be insufficient.
The verified recovery above preserves the failed source operation intact.
