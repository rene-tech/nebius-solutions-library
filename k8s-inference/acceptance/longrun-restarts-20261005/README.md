# Fourteen-day GROMACS execution and large late-state restarts

Status: fourteen-day and bulk-publication releases deployed; the first real
20,000-file trial exposed a publication throughput failure. A measured live
retry is running with the optimized path. Native REST/MCP large-inventory
acceptance is not yet passed. This is not a fourteen-day soak or customer-ready
verdict.

## First deployed twenty-thousand-file trial

On2026-10-05 the reader-first rollout completed with all three API replicas and
the tools at `sha256:ec7ff6833b9e8ccd49cd6ff19e25308019b6bbd0bc1326c1becf83657eeace22`.
The maintenance CronJob used the same fixed image. Internal system/qa operation
`682e77af-3d22-45db-9c3f-29b8515cde70` used a real finite60,000-step MPINAT TPR and
20,000 distinct tiny synthetic retained files. No Lynx key, data or operation
was used.

Initial bundle materialization took2seconds. Native GROMACS2026.2-dev reached
step7,300 in14.954seconds at103.816ns/day and closed generation1 at15:52:57UTC.
The20,007-file checkpoint then exceeded the600-second durable publication
handoff. The source correctly ended failed; the acceptance harness rejected it
because this was a transport timeout, not the intended execution-budget timeout.
It did not pretend the uncommitted checkpoint or the whole test had passed.
Parent DB diagnostics found12,032 finalized output artifacts/3.846MB between
15:53:07 and16:04:01, approximately18objects/second, before customer export.
This pins the initial failed bound to platform artifact publication, not GPU
simulation or customer S3 export.

Private receipts are in
`/home/tux/secure-handoff/fs2-longrun-restarts-20261005/rest-20k-r1/`, including
the exact request, operation status and Pod phase/image/resource history.
The failed resource was released by the normal scientific owner. Evidence is
retained; no customer run was interrupted.

The successor adds sanitized `.fs2/transfer-progress.json` phase/cohort timings
for validation, platform reservation/transfer/finalization, customer export,
manifest commit and acknowledgement. It also batches same-operation checkpoint
restore authorization (128identities) and streams eight SHA-verified downloads,
including safe immutable filename aliases. Scope remains tenant/operation/stage/
shard plus active-attempt/cancellation; a partial or corrupt restore never writes
the restored state/ready marker.74targeted tests and strict typing passed before
the publication repository optimization. The final live rerun is still required.

## Requested scope

- New GROMACS/GROMACS-MPI jobs and explicit continuations default to and allow
  1,209,600 seconds. Infrastructure deadline is 1,211,400 seconds, including the
  existing 30-minute staging/export allowance.
- Preserve the active Lynx recovery and its frozen seven-day native budget.
  Every qualification operation uses the existing internal system/qa key,
  never a Lynx customer key or workspace.
- Support an inventory equivalent to more than fourteen days of default
  five-minute segments, then prove real native checkpoint continuation through
  public REST and MCP and validate all retained files and final target steps.

## Implementation

The attempt-authenticated stage descriptor is reconstructed from frozen durable
state, SHA-256 bound by the Pod, downloaded once into `.fs2/stage-descriptor.json`,
and read by the materializer and collector. The Pod contains no large invocation
environment or file-by-file command-line arguments. Historical inline decoding
remains supported for existing Jobs.

The 32,768-entry manifest envelope reserves up to 32,766 native files. The sizing
basis is 4,032 five-minute segments in fourteen days, four files per segment and
initial-input headroom. Large immutable input-manifest and execution subdocuments
are deterministically Zstandard-encoded and SHA-256 verified. The durable
state/outbox stays limited to 4 MiB; each expanded subdocument and each launch or
checkpoint descriptor is bounded at 32 MiB. SQL-visible scheduling, state,
attempts and identity fields are unchanged; existing immutable-state triggers
continue to compare the exact encoded subdocuments. No database migration is
required. Decode limits are enforced before parsing expanded JSON.

Bulk artifact endpoints reduce repeated whole-state authorization from once per
file to once per bounded batch. Every batch retains the existing tenant,
operation, active-attempt and cancellation checks. Download batches are at most
128 identities; begin/finalize batches are at most 64 identities. File bytes are
streamed with eight transfer workers and individually verified. A failed upload
cannot commit a checkpoint manifest or acknowledgement. Large-file cohorts are
also bounded to 1 GiB except a single larger file. Native explicit filename
expansion permits more than 4,096 trajectory parts while keeping a conservative
system-aware argv byte budget, without shell expansion.

The root integration also owns per-process file-digest caching and customer S3
export optimizations; see the parent release evidence for their measurements.

Generic payload maintenance previously expired any active Operation after the
24-hour request TTL, independently of its scientific execution budget. Active
scientific owners now retain payloads while a durable batch or admission outbox
exists; terminal payload cleanup and unrelated inference TTLs are unchanged.
This does not modify API-key expiry. Both the API and the maintenance CronJob
must use the fixed image. An isolated PostgreSQL16 test exercised the actual
restricted maintenance role before/after outbox materialization and terminal
cleanup without new grants. Seven time-shifted memory cases also passed.

## Offline evidence so far

- 240 focused backend tests passed; four object-store integration tests skipped
  because that separate test backend was not configured.
- 18 large-descriptor/bulk-transfer tests passed, including 20,000-record
  durable/HTTP round-trip, deterministic immutable representations, corruption
  and expansion-limit rejection, cancelled-attempt fencing, ordered bounded
  bulk identities, failed transfer never finalizing, and small legacy state.
  The large inventory uses distinct SHA-256 values, not an artificially
  compressible repeated hash. Late terminal success/failure readers also accept
  the 4-MiB continuation request and 32,766-file native envelope. The final
  descriptor plus native-failure test group passed 42 tests.
- 38 GROMACS worker tests passed, including 4,200 explicit trajectory parts and
  argv/token limits. These are synthetic file inventories, not scientific
  trajectories or a timed fourteen-day simulation.
- Ruff and focused strict mypy checks passed. An existing Starlette 422-name
  deprecation warning remains; it is unrelated to the changed workflow.

## Rollout sequencing

1. Build a temporary compatibility API image from the previous release with
   only new durable codec/model readers. Keep seven-day writers/worker images
   and reject new compressed admissions during this compatibility rollout.
2. Verify all old readers are gone and the compatible fleet is healthy.
3. Activate the complete tested API/tools, single-GPU and MPI wrappers and
   execution map together. Do not admit large-inventory qualification until the
   final fleet is ready. Active admitted jobs keep their frozen plans.
4. Run the existing `../gromacs-continuation-20261005/verify_resume.py` with
   `--padding-files 20000 --resume-seconds 1209600`, an internal finite TPR and
   `--prebuilt-tpr --verify-retained-bytes`. The native MD is real; the additional retained inventory is
   explicitly synthetic. Reuse saved state/idempotency IDs on interruptions.
5. Record exact release digests, source/resumed operation IDs, checkpoint steps,
   retained hashes, export confirmation, transfer timings, failures and cleanup.
   Eight bounded public reads verify actual SHA-256/length of every final
   checkpoint file, retaining local copies and a verification timing receipt.

`prepare_fixture.py` retrieves a previously completed internal 60,000-step TPR
through the public artifact API with byte verification. Its explicit QA-key
check prevents accidental use of customer credentials. Fixture and credentials
stay in the private acceptance directory, not this repository.

`observe_attempts.py --transfer-progress` captures sanitized publication/restore
phase counters from only the exact saved operations' Pods. Committed/restored
phase totals also appear in the collector stdout, without file paths, payloads,
signed URLs or credentials. A terminal failed source stops the acceptance runner
and is retained as a failure rather than silently retried under a passing label.

The subsequent read-side successor also batches128 metadata records for input
admission, signed-download lookup and final stage publication. Every pointer,
access receipt, tenant/operation/stage and successful-attempt check is retained;
the change removes round trips, not verification. Aliases retain request order.
Actual restricted-runtime PostgreSQL qualification published20,000 unique
artifacts, concurrently committed/replayed a20,001-entry manifest including an
alias, and rejected foreign-tenant reads. The artifact/read/batch suite passed
52tests on the isolated disposable database. A separate59-test manifest/handoff
suite passed with its two database cases initially skipped; the52-test database
suite subsequently covered both. Strict typing and Ruff passed.

The recovered-final-publication successor also handles the end of a successful
same-operation retry. Final result records must belong to the successful attempt;
retained earlier-attempt native files are therefore re-homed in bounded 64-file /
1-GiB batches, with SHA/size checks and alias deduplication. This keeps the existing
ownership contract without reverting to thousands of serial upload lifecycles.
The 2,048-file plus alias regression includes later-file corruption and a failed
second batch. Seventy checkpoint/descriptor/failure tests, strict typing and Ruff
passed for this follow-up. No fourteen-day soak is inferred from those tests.

Use `verify_resume.py --source-only` when a source checkpoint must be qualified
before a continuation release becomes available. It saves the same receipt and
stops without submitting continuation work; rerun the identical command without
that flag after the release is ready. This avoids a race with a coordinated
reader rollout and never changes the already admitted native source.

`verify_resume.py --source-receipt <historical-private-state.json>` can qualify
an earlier internal failed operation against a newer continuation release
without rerunning or changing the source. The source must still be owned by
system/qa, match the requested App and have a committed native execution-budget
checkpoint. Current migration fixture: `bfd2bac7-615e-4059-b4b1-3990d48636cc`,
step22,400/60,000 from the prior seven-day-capable worker.

`verify_peer_loss.py --padding-files 20000` admits one internal2x1 MPI study with
an explicitly synthetic late retained inventory, waits for a committed
native checkpoint and injects one UID-fenced rank1 Pod eviction. It requires
unchanged operation identity, infrastructure-retry classification, multiple
attempts, native continuation to the exact finite TPR target, earlier native
file hashes and actual downloaded output validation. Twelve ownership/history
tests cover refusal to target foreign or ambiguous resources. This runner has
not passed live until an explicit receipt is recorded below.

The earlier actual Lynx run is not part of this QA cohort. No automatic
continuation loop or in-place runtime-budget mutation has been introduced.
