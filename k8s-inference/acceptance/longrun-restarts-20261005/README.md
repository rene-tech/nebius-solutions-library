# Fourteen-day GROMACS execution and large late-state restarts

Status: implementation/offline qualification; deployment and native REST/MCP
large-inventory acceptance are pending. Do not interpret this record as a
fourteen-day soak or customer-ready verdict.

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

## Offline evidence so far

- 240 focused backend tests passed; four object-store integration tests skipped
  because that separate test backend was not configured.
- 16 large-descriptor/bulk-transfer tests passed, including 20,000-record
  durable/HTTP round-trip, deterministic immutable representations, corruption
  and expansion-limit rejection, cancelled-attempt fencing, ordered bounded
  bulk identities, failed transfer never finalizing, and small legacy state.
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
   `--prebuilt-tpr`. The native MD is real; the additional retained inventory is
   explicitly synthetic. Reuse saved state/idempotency IDs on interruptions.
5. Record exact release digests, source/resumed operation IDs, checkpoint steps,
   retained hashes, export confirmation, transfer timings, failures and cleanup.

The earlier actual Lynx run is not part of this QA cohort. No automatic
continuation loop or in-place runtime-budget mutation has been introduced.
