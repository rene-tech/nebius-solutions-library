# Long-running GROMACS jobs and checkpoint continuation

The GROMACS and GROMACS-MPI request default and maximum for **new jobs** are
**1209600 seconds (fourteen days) per job**. Explicit smaller budgets remain supported. Infrastructure
retries share the original accumulated runtime budget. A user-requested
continuation is a new operation with a new budget and ordinary tenant admission;
it is not an unlimited automatic retry loop.

## Storage and durability

The engine writes native files in its private worker filesystem. At each closed
segment (five minutes by default, plus the native safe stopping boundary and
upload time), the companion publishes immutable native files and a manifest to
platform storage and, by default, the customer's bucket. The manifest is
committed only after the export succeeds. Local `.cpt` files alone are not proof
of remote durability. An abrupt node loss can lose the unfinished segment, not
the previously committed work.

At a local wall-time limit the worker must terminate GROMACS cleanly and wait for
the final checkpoint upload. It must not pretend the companion received a Pod
termination signal. The result is explicitly `WORKFLOW_TIME_LIMIT_EXCEEDED`,
not scientific success or an unexplained Kubernetes backoff error.

## Continue a failed or cancelled job

Discover committed checkpoints using the same authorized caller as the run:

```http
GET /v1/operations/{source_operation_id}/checkpoints
Authorization: Bearer <platform-key>
```

Then select a job (optional for a single-job run):

```http
POST /v1/operations/{source_operation_id}:resume
Authorization: Bearer <platform-key>
Idempotency-Key: continue-my-run-01
Content-Type: application/json

{"job_id":"production","max_wall_seconds":1209600}
```

The MCP equivalents are `get_scientific_checkpoints` and
`resume_gromacs_workflow`. Poll the **new** operation ID returned by resume;
the old operation remains terminal. Reuse the idempotency key after a lost
response. Do not issue a fresh key merely because the queued run has not started.
Resume uses the same operation-owner access and normal per-key admission as
an ordinary run; no customer key is required or permitted for internal tests.
`202 Accepted` means durable admission, not completed restoration or successful
MD. Use `GET /v1/operations/{new_operation_id}` and its eventual result/artifacts
to determine completion. See the [scientific API contract](../../../docs/SCIENTIFIC_BATCH_API.md).

Continuation preserves the original TPR, native checkpoint, bias/restart files
and previous trajectory parts. It skips completed workflow commands; it does not
rerun preparation, regenerate velocities, silently change physics, or restart
from the original coordinates. The worker uses native `-cpi` and `-noappend`.
Completed trajectories are ensemble simulations, not bitwise-reproducibility
claims across different GPU shapes or engine builds.

This is **native `.cpt` recovery**, not CUDA/CRIU GPU-process snapshot restore.
It restages durable files and starts a compatible GROMACS process on the admitted
shape. No measured GPU-snapshot cold-start time or transparent change from an
existing TCP run to RDMA follows from a successful continuation. Retain the
frozen execution/recipe identity and verify any new shape separately.

Files are referenced in a verified per-file input manifest and streamed directly
from object storage into the new worker. They are **not** repacked on the API
server and are not subject to the compressed upload-bundle limit. Existing
workspace byte/file bounds and customer storage quotas still apply. Native
trajectory/log parts are retained; old wrapper segment logs stay with the source
operation. `_fs2-continuation.json` records immutable source/checkpoint lineage.

Late restarts use a SHA-256-bound, attempt-authorized descriptor fetched from
durable state into the private workspace. Neither the file inventory nor the
materialization commands travel in process environment/argv. Downloads use
128-file authorization batches and at most eight streamed transfers; checkpoint
uploads use 64-file/1-GiB cohorts (one larger file may be a cohort by itself) and
at most eight streamed transfers. Each file keeps its immutable digest and byte
count. Any failed transfer prevents the checkpoint-generation acknowledgement.

The bounded inventory allows 32,766 native workspace files plus result/manifest
slots. Fourteen days at the default five-minute interval imply 4,032 segments;
the envelope reserves four files per segment plus initial-input headroom. This
is not an unlimited file count, and extra customer outputs still count against
it. Large immutable input/execution subdocuments use deterministic,
SHA-256-verified Zstandard encoding inside the existing 4-MiB durable state/outbox
envelope. Expanded metadata is bounded at 32 MiB per subdocument; descriptors
and checkpoint manifests are bounded at 32 MiB. Large incompressible requests
can still be rejected at admission. Scheduling/status/attempt fields remain
ordinary queryable PostgreSQL JSON. Existing small admitted plans retain their
original representation.

See [current long-run release evidence](../../../acceptance/longrun-restarts-20261005/README.md)
for the exact tested inventory, interfaces and deployed identities. The
[earlier incident record](../../../acceptance/gromacs-continuation-20261005/README.md)
is retained as historical evidence, not current large-inventory qualification.

## Release qualification

Changing a fourteen-day bound does not constitute a fourteen-day soak test. Qualify the
exact engine wrapper and backend images using bounded real timeout → committed
remote checkpoint → new-operation resume → native completion tests. Check the
native resume step against the saved checkpoint, complete output inventories,
earlier trajectory parts, REST/MCP parity, idempotency and tenant isolation.
Keep failures and untested execution shapes explicit in the release record.

The infrastructure deadline allows an additional 30 minutes for staging and
final export; it is not an additional simulation budget. Roll out readers that
understand this deadline and compressed metadata before admitting plans
containing them. Preserve frozen plans for existing runs and never change a
customer's original terminal record. Do not roll back to pre-compatible readers
while such plans exist, even if no workload is currently running.

Changing the new-job default does not extend an existing worker's frozen budget.
The already-running Lynx recovery retains seven days; its checkpoint can be
continued with a fresh fourteen-day budget if it reaches that boundary. Patching
only a Kubernetes Job deadline would not extend its native runtime budget.

## Lynx incident motivating this fix

Operation `aa502153-3c75-422c-8040-82461fdbfcaa` reached the old default six-hour
budget on 2026-10-05. The last committed generation was 71 at step 13,963,440
(27.92688 ns), with 70 completed trajectory segments. The intended run was
500,000,000 steps / 1 µs. At the observed roughly 116 ns/day it needs about
8.6 days in total before platform overhead. The existing seven-day continuation
may still need a final explicit continuation. These are observed/estimated facts,
not a completed recovery claim.
