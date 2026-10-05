# Long-running GROMACS jobs and checkpoint continuation

The GROMACS and GROMACS-MPI request default and maximum are **604800 seconds
(seven days) per job**. Explicit smaller budgets remain supported. Infrastructure
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

{"job_id":"production","max_wall_seconds":604800}
```

The MCP equivalents are `get_scientific_checkpoints` and
`resume_gromacs_workflow`. Poll the **new** operation ID returned by resume;
the old operation remains terminal. Reuse the idempotency key after a lost
response. Do not issue a fresh key merely because the queued run has not started.

Continuation preserves the original TPR, native checkpoint, bias/restart files
and previous trajectory parts. It skips completed workflow commands; it does not
rerun preparation, regenerate velocities, silently change physics, or restart
from the original coordinates. The worker uses native `-cpi` and `-noappend`.
Completed trajectories are ensemble simulations, not bitwise-reproducibility
claims across different GPU shapes or engine builds.

Files are referenced in a verified per-file input manifest and streamed directly
from object storage into the new worker. They are **not** repacked on the API
server and are not subject to the compressed upload-bundle limit. Existing
workspace byte/file bounds and customer storage quotas still apply. Native
trajectory/log parts are retained; old wrapper segment logs stay with the source
operation. `_fs2-continuation.json` records immutable source/checkpoint lineage.

The incident fix qualifies the observed multi-part customer case and bounded
native REST/MCP tests, not arbitrarily large restart inventories. Very large
file lists remain subject to the durable admission and Kubernetes launch-envelope
bounds. See the [exact release evidence and remaining limits](../../../acceptance/gromacs-continuation-20261005/README.md)
before treating a metadata-only test as proof of a thousands-of-files launch.

## Release qualification

Changing a seven-day bound does not constitute a seven-day soak test. Qualify the
exact engine wrapper and backend images using bounded real timeout → committed
remote checkpoint → new-operation resume → native completion tests. Check the
native resume step against the saved checkpoint, complete output inventories,
earlier trajectory parts, REST/MCP parity, idempotency and tenant isolation.
Keep failures and untested execution shapes explicit in the release record.

The infrastructure deadline allows an additional 30 minutes for staging and
final export; it is not an additional simulation budget. Roll out readers that
understand this deadline before admitting plans containing it. Preserve frozen
plans for existing runs and never change a customer's original terminal record.

## Lynx incident motivating this fix

Operation `aa502153-3c75-422c-8040-82461fdbfcaa` reached the old default six-hour
budget on 2026-10-05. The last committed generation was 71 at step 13,963,440
(27.92688 ns), with 70 completed trajectory segments. The intended run was
500,000,000 steps / 1 µs. At the observed roughly 116 ns/day it needs about
8.6 days in total, so a seven-day continuation may still need a final explicit
continuation. These are observed/estimated facts, not a completed recovery claim.
