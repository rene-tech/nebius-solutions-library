# Customer trial remediation and repeat acceptance

Status: r01 completed **14/14 scientific operations**, but **overall acceptance
is not yet clean**. Both follow-up fixes are deployed from `5f5061b28`, and the
focused live admin phase/download check passed. Full r02 started20:10:47UTC;
two clean complete cohorts remain required. See the [follow-up release](FOLLOWUP-RELEASE.md),
[r01 workload results](workload/REPORT-r01.md)
and [whole-window observation](observer/REPORT-r01.md).
The original failed cohort is preserved unchanged in `../customer-trial-20260907`.

The customer scenario uses the nine already qualified scientific profiles,
including all five primary models, with unchanged non-production fixtures.
Each full cohort submits fourteen operations: cross-model sequential requests,
then mixed bounded concurrency, a four-shard bulk request and higher-priority
requests. Ordinary Qwen HTTP/MCP traffic and authenticated admin observation run
concurrently. Client slot waiting is distinct from server queue waiting.

Acceptance requires two consecutive clean full cohorts after the final deployed
fix: every operation succeeds, result bytes verify, background traffic has no
unexpected errors, and no manual intervention is needed. Admin checks must
include live progress, meaningful pending reasons, consistent accounting,
snapshot capability and authorized artifact downloads. Deliberate negative
authorization tests are not availability failures. No automatic submission retry
may conceal a failed customer request. A 10/10 rating, if earned, applies only to
this bounded tested scenario, not a general availability or scale SLA.

## Corrections

- [Route continuity and MCP errors](MCP-REMEDIATION.md).
- GPU placement checks the complete Pod CPU/memory/GPU envelope against each
  compatible node, including collectors, init containers and overhead. Existing
  capacity ceilings and qualified scientific runtime recipes are unchanged.
- Admin data/actions and repeat-browser checks are tracked in the parallel admin
  lane; live results will be recorded with the deployed source/image identities.
- Scientific scheduler conditions produce bounded, nonterminal pending codes in
  the existing lifecycle event stream. A reason arriving after the initial
  pending phase is retained; identical replays deduplicate by event identity.

## Restore accounting boundary

Scientific snapshots restore inside the `scientific-stage` container, not an
init container. Counting its entire lifetime as active execution was wrong.
The controller now reads the existing, exact `scientific_snapshot_request`
supervisor marker from bounded timestamped Pod logs and separates:

1. Stage container start to marker: supervisor preparation and CUDA/CRIU restore
   attempt, reported as the restore phase. This is **not** CUDA-copy-only time.
2. Marker to stage completion: scientific request execution. Ordinary model
   loading after a failed restore remains part of the fallback request.

Both `cuda-criu-restored` and `normal-load-fallback` delimit an actual attempted
snapshot startup. The interval alone does not claim successful snapshot use;
the marker's mechanism and workload result evidence determine that separately.
Missing/malformed/out-of-range markers leave the split unknown, while factual
scheduler/device occupancy remains available. No speculative compute interval
is inserted and later silently reclassified. Cached markers are scoped to Pod
UID and bounded to 2,048 entries; log reads are limited to 256 KiB/2,000 lines.

The existing namespace-scoped scientific controller role gains `get pods/log`
for this measurement. There is no new workload token, capture source change,
driver change, recapture, broad audit or security-policy project.

## Verification so far

The first release passed 1,594 non-external backend tests, 19 isolated PostgreSQL
tests and 149 UI tests. Its complete live r01 ran 24m52.891s: all fourteen
scientific operations succeeded, 28 artifact downloads verified and 1,004
scientific HTTP calls had no errors. All 66 sampled ordinary HTTP/MCP requests
succeeded. BindCraft placement, automatic capacity growth and three automatic
priority-preemption recoveries were observed.

This is not a clean overall result: whole-window logs show two approximately
five-second Qwen route withdrawals during burst scale-down, despite its hot
Pod staying ready. The separate admin phase table showed restore0s from ingestion
clocks, although the ledger correctly recorded3.946846 restore GPU-seconds.
Follow-up code retains fixed hot routes during the narrow asynchronous idle
acknowledgement and projects actual phase-signal wall-time unions; no ingestion
or GPU-second-to-wall-time inference. Runtime qualification hashes are unchanged.
Focused follow-up routing tests107passed; admin backend100passed including
populated PostgreSQL, UI149passed. Full non-external backend suite1611passed,
4optional skips and76external-service tests deselected in222.09s. R01 is preserved and does not count toward the clean
streak. Academic Pod CPU/RAM was missing from r01's two filtered PromQL queries;
the report corrects that scope, and future cohorts include the namespace.

The private credential bundle, raw logs and Terraform states are never committed
with this evidence. Customer elapsed times are not fresh-node cold-start
benchmarks or scientific quality claims.
