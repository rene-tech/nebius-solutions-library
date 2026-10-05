# Fourteen-day GROMACS execution and large late-state restarts

Status: fourteen-day execution and the large-inventory transport are deployed.
A real 20,007-file source checkpoint committed within the existing 600-second
handoff. A new fourteen-day-budget continuation from the old seven-day worker
has passed, including all output bytes. Large native REST continuation also
passed with all 20,031 final files downloaded and SHA/size verified; raw MCP
also passed that complete byte verification. The subsequent concurrent REST/MCP
cohort failed both the publication deadline and availability criteria. Their
combined pipeline/responsiveness successor has now passed concurrent native
REST/MCP continuation, terminal publication and complete all-reader readiness.
Full SHA/size verification of all 40,062 output files also passed; MPI peer-loss
qualification remains a separate pending gate.
This is not a fourteen-day soak or customer-ready verdict.

## Bridge-872b concurrent native and availability pass

Both internal continuations ran on the exact API/collector
`sha256:872b7d58cf275f2bc7e7d396e626fd1418285e65f25047ed8fbd35ab7af9f6dd`,
source `dbcb4b994`, with the unchanged `5acd77d6` single-GPU worker. The parent
verified every reader, controller and maintenance instance before admission.
One L40S per operation, the same original `33b398b6` source, and the original
system/qa concurrency of two were used. No customer key or limit changed.

| Measurement | REST r6 | Raw MCP r3 |
| --- | --- | --- |
| Operation | `2da99333-e473-4025-8230-edf67d49c62b` | `ba3e658b-6355-418b-b7fd-d4bd8c2b1067` |
| Initial / exact replay admission | 15.077 / 11.883 s | 19.515 / 18.251 s |
| Input materialization | 114 s | 107 s |
| First 20,011-file platform publication | 170.320 s | 191.073 s |
| First verified customer export | 155.209 s | 158.953 s |
| First complete durable handoff | 331.050 s | 354.805 s |
| Later incremental handoffs, generations 2–6 | 4.879–5.944 s | 4.989–6.342 s |
| Final handoff including complete remote audit | 42.959 s | 43.831 s |
| Public terminal success, UTC | 19:29:57.317 | 19:31:21.062 |
| Final files actually downloaded and SHA/size checked | 20,031 | 20,031 |
| Verified output bytes | 23,042,682 | 23,042,957 |
| Eight-stream artifact verification | 1,681.657 s | 1,665.128 s |
| Extra bounded artifact GET attempts | 4 | 4 |
| Verification completed, UTC | 19:58:07.775 | 19:59:31.633 |

Native logs confirm exact continuation from step 7,800 and the final native
checkpoint confirms 60,000 steps/zero exit for both. Each final manifest contains
20,031 files. The original 600-second handoff bound was never relaxed. The normal
owner released the exact Pods; absence was observed at 19:29:50 and 19:31:08 UTC.

The pre-armed three-reader capture from 19:18:06 through 19:31:46 retained 978
successful readiness responses, zero non-200 responses, and no coverage gaps.
An independent response-body sampler also recorded 489 healthy samples by that
cutoff. This is an observed cohort pass, not an unlimited availability guarantee.
All native/publication/readiness and these artifact GETs completed before the
parent's next release activation. Both complete byte-verifiers exited zero and
preserved 20,004 source files each. All eight extra GET attempts were measured;
there were no unknown retry counts, reused local files, or hash/length failures.
Do not describe this as a zero-retry run. Download overhead remains substantial
for tens of thousands of tiny files even though native checkpoint publication
now fits comfortably inside its unchanged bound.

Private receipts are `rest-20k-r6`, `mcp-20k-r3`, and
`concurrent-20k-r2/native-terminal-readiness.json` under
`/home/tux/secure-handoff/fs2-longrun-restarts-20261005/`. Every failed predecessor
is retained below; a later pass does not erase it. The reusable acceptance harness
also saves public failed terminal envelopes before rejecting a failed case.

## Concurrent release-191e failure and bounded pipeline follow-up

The exact schema-38 API/collector release `191e2c2b` admitted two internal
20k-file continuations, staggered initial/replayed submission and then overlapping
materialization/publication. Both used the same preserved `33b398b6` source,
one L40S each, the unchanged system/qa concurrency of two, and no customer key.

| Case | Operation | Initial/replay | Materialization | Platform publication |
| --- | --- | --- | --- | --- |
| REST r5 | `7fb06cae-0eb6-44ab-8f5d-772d2fb3ae86` | 33.882 / 30.778 s | 171 s | 551.456 s |
| MCP r2 | `c2d128a9-80f5-418f-82a6-0372b4fdb7a7` | 25.576 / 21.975 s | 163 s | 456.154 s |

The REST publication spent 224.974 seconds reserving upload handles, 263.992
finalizing metadata, and only 54.547 transferring bytes. MCP spent
175.952/228.680/43.702 seconds respectively. Both then entered customer export
but exceeded the unchanged 600-second handoff without a committed acknowledgement.
Their public results are failed (`Error` and `PodFailurePolicy`); normal owners
released the Pods by 18:50:22 and 18:51:01 UTC. The original safe checkpoint
remains available. These are platform transfer failures, not evidence that the
scientific trajectories or fourteen-day continuation completed.

Readiness also failed with captured `database_unavailable`/database-readiness
timeouts at 18:35:35.409 and 18:37:33.843 UTC. At those times the parent's
database observer saw approximately 0.5 CPU rather than the prior database
saturation. The sibling's measured artifact-inventory responsiveness fix is
commit `05712815f`; its live availability gate remains open. Private receipts
are in `rest-20k-r5`, `mcp-20k-r2`, and `concurrent-20k-r1` under the private
acceptance directory.

The follow-up overlaps at most two metadata cohorts while retaining the existing
global eight PUT streams per collector, at most 128 signed handles, and at most
1 GiB of in-flight file bytes (a larger individual file runs alone). It covers
both checkpoint publication and final successful-attempt rehoming. Results are
consumed in deterministic input order, aliases are preserved, and returned
SHA/length must still match the stopped native inventory. Failure/cancellation
stops new cohorts and drains running transfers before propagating; no partial
customer manifest, final result, or native acknowledgement is committed.

Progress now distinguishes the non-overlapping wall timeline (`phase_seconds`,
including `platform-pipeline`) from overlapping summed cohort work
(`cohort_phase_seconds` and `active_cohort_phases`). These sums must not be
misreported as elapsed wall time. Eleven new tests plus existing checkpoint and
descriptor tests passed: 57 total, covering GROMACS and MPI, checkpoint/final
publication, aliases, exact retry identity, global eight-stream concurrency,
byte bounds, mutation, partial failure, and cancellation/draining. Focused strict
typing and Ruff passed. No timeout, quota, customer limit, or native image change
was used to make the test pass. Parent owns the combined rollout and rerun.

## REST r4 success and remaining availability defect

Internal operation `d2e5befa-367f-453d-94f4-cee8bca045bb` resumed the saved
20k-file source at native step 7,800 and finished the original 60,000 steps on
API/collector `64c5c77d` and worker `5acd77d6`. It became publicly succeeded at
17:57:30 UTC; the normal controller released its Pod. All 20,031 final files
(23,042,709 bytes) were downloaded over the public artifact API and checked
against SHA-256 and length, preserving 20,004 immutable source files. Eight
parallel reads took 1,272.759 seconds. This older verifier did not retain its
download retry count, so no zero-retry claim is made. The subsequent MCP
verifier records each file's transfer attempts in a private append-only ledger.

Materialization took 111 seconds. The first native handoff committed in
544.692 seconds, including 390.811 seconds platform publication and 153.050
seconds customer export. Later unchanged-history handoffs took about six
seconds. The unchanged 600-second handoff deadline was never relaxed.
The initial/idempotent admissions took 34.161/29.360 seconds and reused one
operation. Its fourteen-day budget is a contract test, not a fourteen-day soak.

Readiness still failed: four 503 responses were captured across the three
readers during admission/verification. A task-owned response-body sampler
confirmed `database readiness check timed out`; a successful native result
does not erase this failed availability criterion. Private evidence lives in
`/home/tux/secure-handoff/fs2-longrun-restarts-20261005/rest-20k-r4/`.

Parent investigation found an expensive pending-batch claim scan and built the
matching partial index online at 18:03:59 UTC without changing schema ledger 37.
After that, raw MCP continuation `c9d6195b-8b69-4296-b1a3-c5596083f050` admitted
in 30.423 seconds, replayed in 37.100 seconds and again produced database
readiness timeouts. Its first large handoff nevertheless committed in 547.174
seconds (369.298 platform plus 177.324 customer export). Native execution exited
successfully at 18:26:25 UTC; the public result is succeeded and the Pod was absent
at 18:26:48. Full public byte verification passed: all 20,031 files, 23,042,067
bytes, and 20,004 immutable source files, with the original target of 60,000
steps reached. Eight parallel reads took 1,270.784 seconds and required six extra
GET attempts, retained individually in `retained-artifact-transfers.jsonl`.
No local files were silently reused and no retry counts are unknown. The admitted
runtime stayed on `64c5c77d`; artifact downloads crossed the compatible `191e2c2b`
rollout, so this is explicitly mixed-reader download evidence, not a single-image
claim. Two admission-time readiness 503s remain an availability failure.

Current Kubernetes log files rotated away some admission-time probe records.
`capture_api_readiness.py` now explicitly reports incomplete cohort starts,
tails or gaps as unknown rather than treating a healthy remaining tail as a
pass. The independently pre-armed response-body sampler retained the actual
admission failures. Seven sampler/selector/coverage tests pass.

The next observed cause was nine concurrent PostgreSQL client backends sending
the same large admission-outbox page. The background workers each fetched and
decoded identical records, with the remaining JSON decoding on the API loop.
Commit `7a37a741c` moves get/list decoding onto the existing bounded scientific
CPU executor and uses one nonblocking PostgreSQL advisory owner for background
recovery across replicas. A local flag avoids worker-level pool contention;
normal API submission remains independent. Existing frozen-state comparison
still arbitrates the original-submit/recovery race. No pool, timeout, key,
quota or customer limit changes are introduced. Pool reset drains even repeated
raw cancellation; disconnect releases the advisory owner automatically.

Forty-five targeted tests passed against an isolated actual PostgreSQL database
and MemoryStore, including nine competing recovery workers reading/materializing
one page, off-loop heartbeat, original-submit race, callback atomicity,
cancellation, disconnect reacquisition and fourteen-day payload retention.
Strict typing and Ruff passed. Parent owns the combined reader/index/metrics
release and its live availability rerun. No standalone deployment was performed.

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

### Old-worker compatibility, with concurrent large-restart traffic

On the exact API/collector release
`sha256:64c5c77d4eb6cac4b4ecf58d16766b2aa0423f836c1b9adee5a2034abd3e3644`,
internal operation `5f710396-0dc8-4ea8-b3d1-fa0c24c87f85` continued the saved
`bfd2bac7-615e-4059-b4b1-3990d48636cc` checkpoint. The source was qualified on
the old `f633539e` seven-day-cap worker; the new worker is `5acd77d6`.
Native GROMACS resumed at step 22,400 and finished the original 60,000-step
target with a requested 1,209,600-second budget. All 29 final files, totaling
22,330,900 bytes, were downloaded and checked against their size and SHA-256;
eight immutable prior files were preserved. Customer-bucket export and exact
idempotent replay passed. Initial and replay admission took 3.522 and 2.381 s.
The normal owner released its Pod without manual cleanup.

This case ran concurrently with the 20k-file REST r4 materialization/publication,
using the existing system/qa second lane. All 175 readiness probes captured
across the three readers during the small case passed. This does **not** erase
the three earlier r4-admission readiness 503s. The original active Lynx operation
was neither tested nor changed. Receipts are private under
`/home/tux/secure-handoff/fs2-longrun-restarts-20261005/legacy-seven-to-fourteen/`.

`sample_readiness_reasons.py` provides bounded read-only localhost sampling of
the exact three existing readers. It retains status/timing and an error's public
type/message only, never successful response bodies or customer requests.
The sampler and existing log selector passed five targeted privacy/selection
tests. Readiness-body sampling supplements, rather than replaces, the complete
all-reader probe capture.

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

## Live follow-up: large input state is part of the workload

Source retry `33b398b6-4311-4400-9a0b-67dfa2e596b5` committed a real native
checkpoint at step7,800/60,000 with20,007 files and an8,324,151-byte manifest at
16:39:38.759UTC. Platform publication took332.78seconds; the whole handoff,
including verified customer export, was about490seconds and stayed below600.
It then terminated with the deliberately requested execution-budget timeout.
The count/identity inventory is synthetic; native GROMACS execution and checkpoint
continuation are real. This is not a fourteen-day scientific soak.

Its explicit continuation `5dfc4c25-8747-403f-8ec6-07525a1ade98` admitted on the
979e85fa reader release, but the API responded202 only after65.794seconds. The
caller had already received a non-JSON failure. The accepted operation was found
through its exact server request and read-only idempotency lookup; no duplicate
was submitted. Read-only follow-up uses `--observe-existing-resume`, which reports
only output validation, not a passing admission/idempotency experience.

This caught two additional scale-dependent CPU paths. Controller materialization
resolution scanned all inputs once per file. A logical-ID index and bounded
cooperative yields changed a local20k-file profile from10.53seconds to1.34seconds;
111controller/production tests plus typing/Ruff passed. The original test first
failed only because the new test called the destination field `target_path`;
the assertion was corrected to the actual `destination` contract.

Every artifact batch also repeatedly reconstructed the same frozen20k-input
state. A process-local cache now retains only fully validated immutable input
and execution dataclasses, keyed by SHA of every encoded byte and its decoder
context. It is bounded to four entries and64MiB of recursively measured retained
object graphs. Mutable status, tenant access, attempts, cancellation and their
validation are still read fresh on every request. Cold/warm local decode was
0.739/0.0056seconds with18.26MB retained for the20k case. Corruption, changed plan,
tenant mismatch, byte/count eviction, immutable fields, legacy decoding and warm
20k cancellation/replaced-attempt rejection are tested.47codec/descriptor/capability
tests and11final cache-focused tests passed, with strict typing and Ruff.

The continuation's materializer finished20k immutable files in333seconds before
this cache release. Its first native publication exposed the repeated-decode
bottleneck again (4,800/20,011files after354seconds), so the complete REST/MCP
late-inventory verdict remains pending a genuine fixed-release pass.

At 16:58 UTC the native worker correctly reported `durable checkpoint handoff
timed out`. The companion had finalized only 8,256 of 20,011 files after 604.6
seconds: 281.2 seconds reserving artifacts, 290.7 finalizing them and 26.9
transferring bytes. The unchanged 600-second worker bound was not extended to
hide that failure. The companion's normal finalization remains observed; the
original source checkpoint is safe and can be retried under a new explicit
continuation identity. Future acceptance receipts include actual initial and
idempotent-replay REST/MCP admission durations, not only GPU execution times.

The full-node MPI recovery case is held during the separately authorized
two-node InfiniBand reprovisioning. Single-GPU REST and raw-MCP continuation use
the existing `gromacs` App and do not require or interfere with that cutover.

## Availability and publication follow-up

Continuation retry `59ae91d5-be0b-4e29-a7b6-151c7b7a4631` used the frozen
`ea48e966` release. Initial admission returned HTTP 202 in 29.280 seconds;
same-key replay returned 202 in 15.029 seconds and reused the operation. A full
reader log capture found four readiness 503s: all three readers briefly degraded
at initial admission (17:07:31–35 UTC), plus one at 17:19:05 during publication.
Input materialization improved from 333 to 132 seconds. These improvements did
not constitute acceptance: platform publication reached 20,011 files only after
599.69 seconds, leaving no time for customer export inside the 600-second native
handoff. Native execution failed, the public failure was published, and the GPU
was released normally. No failure was hidden through a changed deadline.

The next source revision moves existing pure metadata validation, compilation,
serialization and decoding to a dedicated two-thread executor. It does not
remove preflight, cache authorization or change idempotency/admission rules.
Raw and repeated task cancellation drains running callbacks before their owner
can leave its transaction or lock; queued work can be cancelled before starting.
The pool is process-wide and works across multiple event loops. Local profiles
found 4.24 seconds in large input validation (3.67 in JSON Schema) and 2.59 seconds
in a single plan compilation. These had previously run on the API event loop.

Artifact batches additionally reuse only the binding tuple and digest derived
from already-validated frozen input and invocation objects. Strong references
prevent object-ID reuse; retained object graphs are bounded to 64 MiB and four
entries. HMAC verification and current tenant, attempt, status, cancellation and
access checks still run for every request. Changed source objects or a changed
signed digest cannot reuse an authorization decision, because none is stored.

Using the exact 20,006 file references and paths from source `33b398b6`, local
fresh-state decoding plus authorization measured 1.075 seconds cold and 0.425
seconds for 50 warm calls (8.51 ms/call). The binding cache retained 20.23 MB.
The earlier representative 20k profile spent 75.8 ms/call on binding construction
and hashing alone. These are CPU measurements, not database/network or live
handoff claims. Fresh duplicate checks and state validation remain enabled.

Validation: 119 existing local cases passed (three unconfigured database skips,
two database cases deselected), then 120 cases passed with the actual disposable
PostgreSQL instance, including 20k outbox reopen/cancellation/SQL immutability,
artifact reads, callback transaction safety, controller and checkpoint behavior.
Eleven new binding-cache/actual-manifest heartbeat cases passed. The standalone
executor also passed five cancellation, multiloop, context and bounded-concurrency
cases. Strict typing and Ruff passed. The next live REST/MCP verdict is pending.

`capture_api_readiness.py` collects only each exact current reader's readiness
responses and the selected internal source's resume timings. It excludes request
bodies, credentials and unrelated customer requests. Missing reader/probe evidence
is unknown, not a pass; two selection/redaction tests passed. The r3 receipt is
`rest-20k-r3/api-readiness.json` in the private acceptance directory (877 probes).

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
