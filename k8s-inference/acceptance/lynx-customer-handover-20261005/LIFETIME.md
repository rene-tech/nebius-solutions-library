# Bounded GROMACS lifetime check — 5 October 2026

This is a source/retained-metadata review for the Lynx continuation, not a
fourteen-day soak or a promise against cloud failures. No live policy, key,
quota, customer Job or bucket is changed by this work.

## Budgets that actually apply

| Layer | Bound and meaning |
| --- | --- |
| New workflow and explicit `:resume` | Default/max 1,209,600 s (14 days), including native checkpoint I/O. A new explicit continuation gets a new budget. |
| Already-admitted customer recovery | Frozen original 7-day budget; changing defaults does not rewrite it. |
| Infrastructure retry | Restores the same workflow state and subtracts accumulated elapsed time; it does not reset the budget indefinitely. |
| GROMACS/MPI Kubernetes Job | Source execution map 1,211,400 s (14 days + 30 min) for staging/export; controller does not equate its 30 s reconciliation lease with run lifetime. |
| Collector | Job deadline less 120 s; controller's post-model-exit collection grace is 1,800 s. Neither removes the native per-handoff bound. |
| Native restore/checkpoint acknowledgement | 600 s per handoff. Failed transport wakes the engine with an explicit error; successful publication alone acknowledges a generation. This bound is unchanged. |
| SIGTERM | Native process gets a safe-boundary stop; worker waits up to 90 s in its timeout path, within the 120 s Pod termination grace. Abrupt node loss can still lose the uncommitted segment. |
| HTTP transfer | Companion httpx timeout 60 s per network operation; 5 data attempts, existing exponential backoff, at most 8 streamed platform PUT lanes. These are not the overall simulation deadline. |
| Signed artifact handle | Default 600 s, maximum 900 s. Renewal fix below prevents retry/queued-transfer reuse of expired handles; expiry remains short. |
| Workload capability | Bound to current tenant/operation/attempt, not a short wall-clock expiry. Authorization still rechecks active attempt, cancellation and immutable ownership. |
| Customer S3 export | Scoped S3 credentials are obtained in memory through the current attempt. Content-addressed objects are deduplicated and the generation manifest is written last. Standard S3 retries remain bounded. |
| Payload/history maintenance | Active scientific request payloads are excluded from generic 24 h cleanup; scientific records are excluded from generic 7-day operation deletion while referenced. Terminal artifact retention defaults to 90 days. |
| Client wait/polling | A local wait timeout or disconnected notebook does not cancel server-side work. Persist operation/idempotency IDs and query state before replaying. |

Scientific submission is not the generic synchronous serving path's one-day
request-deadline header. Do not add that header and then mistake it for a
fourteen-day native job setting. Queue policy is frozen at admission; pending
capacity and configured queue deadlines remain separate from execution time.

Referenced source: [contracts](../../models/molecular-dynamics/gromacs/runtime/fs2_gromacs/contracts.py),
[worker](../../models/molecular-dynamics/gromacs/runtime/fs2_gromacs/worker.py),
[execution map](../../catalog/runtime/contracts/scientific-execution-map.json),
[collector/models](../../components/control-plane/src/fs2_serve/scientific_batch/models.py),
[companion](../../components/control-plane/src/fs2_serve/scientific_batch/companion.py),
[storage export](../../components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage.py),
[retention regression](../../components/control-plane/tests/test_scientific_payload_retention.py).

## Concrete fixed defect: stale transfer handles

Before commit `a4277f45f`, prepared downloads and bulk-upload reservations reused
the same signed URL across queue waits and retries even after its expiry.
The correction renews that **one file**, through the existing authorized route,
when its handle has ≤5 s remaining or demonstrably expires during a failed
object-store transfer. It preserves the same artifact UUID, digest, byte count
and media type, or the same deterministic upload ID and immutable reservation.

A still-valid handle's 403 is not retried. Revoked attempt/auth failures, wrong
identity/digest and stale attempt conflicts remain errors. A renewal does not
reset the existing five-data-attempt budget, increase URL TTL, add threads or
change 600 s handoff limits. Streamed downloads retain digest verification and
atomic final-file replacement; upload files retain change detection.

Evidence: 28 new clock-controlled cases plus 91 existing workspace, routing,
descriptor, native checkpoint and two-cohort pipeline regressions passed
(119 total); Ruff passed. Tests cover queued transfer expiry, expiry during a
request/backoff, immutable identity mismatch, 401/403/409 renewal denial and
the five-attempt maximum. This is source qualification; deployment and live
continuation qualification are recorded by the integrating release, not
assumed from unit tests.

## 600 s handoff: actual bytes, not a larger timeout

The approved generation 71 metadata has 305 files / 82,674,112 bytes. Moving all
those bytes within 600 s requires only 0.138 MB/s at the data layer, but this omits
authorization, hashing, per-object overhead, platform verification and customer
export. The real demo receipt must measure restore and publish elapsed times,
new bytes vs unchanged files, and metadata/transfer/export phases. Do not call
a 600 s handoff safe using bandwidth alone.

Steady-state publication uploads changed content, not all prior trajectory
parts again. Full restore and the first export in a new operation revisit the
complete history. A growing manifest and many small files can dominate even
when byte throughput looks adequate. The two 64-file metadata cohorts share 8
PUT lanes; the final handoff remains acknowledged only after all required
publication succeeds. No timeout was stretched to hide a slow publication.

## Storage projection for this exact cadence

Source metadata only; no customer scientific content is in this document:

| Current generation 71 content | Bytes |
| --- | ---: |
| Fixed inputs/structure/topology/TPR/index/MDP |34,044,733 |
| Current+previous native checkpoint |8,907,992 |
| 70 energy parts |9,336,304 |
| 70 trajectory parts |19,424,220 |
| Native/wrapper logs |10,960,863 |
| Total latest workspace inventory |82,674,112 |

At the requested **measured** 200 ns/day, the remaining
`1000 − 27.92688 = 972.07312 ns` takes about 4.86 days. Five-minute segments imply at
most roughly 1,400 additional segments before stop/export overhead. With the
same TPR output cadence:

- Scale energy+trajectory bytes by added ns; scale logs by segment count.
  Retain fixed inputs and two current checkpoints, not one copy per segment
  in the local workspace. Including a final structure gives about 1.32 GB;
  doubling new output growth for headroom gives about 2.54 GB, below the 4 GiB
  request default. Concatenated analysis outputs or changed output frequency
  need their own allowance; the public maximum is 48 GiB, not the bucket quota.
- The customer bucket retains **historical distinct objects** as well as the
  latest manifest. Conservatively allow two new 4,453,996-byte checkpoint
  versions per segment: about 12.47 GB for 1,400 segments. This deliberately
  overcounts a previous-checkpoint object already deduplicated by its digest.
- Every generation retains its own growing manifest. Approximating four new
  file entries/segment from the current 130,375-byte/305-entry manifest gives
  about 1.86 GB of manifest history across 1,400 segments. Paths/metadata can vary;
  this is an estimate, not a quota guarantee.
- Workspace/output content plus those conservative checkpoint and manifest
  histories is about 15.65 GB for the remaining production run. Lynx 100 GB has
  preliminary room **only if current bucket usage and other runs leave that
  space**. Another imported operation stores content under another prefix;
  do not assume cross-operation deduplication. Fourteen full days can retain
  about 35.92 GB of checkpoint versions alone with the same conservative rule.

The current demo task is bounded differently: a short qualification plus a
six-hour soak. 72 five-minute segments retain at most about 0.64 GB of checkpoint
versions under the same conservative estimate, plus inputs, outputs and
manifests. That can fit the existing 5 GB demo bucket, but actual occupancy must
be observed. A full 1 µs run must not be left running there based on the small
initial copy alone. No canonical seed data is modified.

## Exact remaining claims and conditions

The fixed source removes a predictable **signed-handle expiry** failure and
the already-deployed fourteen-day path removes the former default six-hour
compute stop for new runs. It does not guarantee zero infrastructure failures,
infinite waiting, unlimited storage or indefinite retry.

Final acceptance still needs the exact-release public demo continuation,
replayed admission, ≥200 ns/day delivered work, preservation of old native
history, observed transfer margins, and the actual six-hour soak. Retain any
unexpected failure rather than relabel it as success. Use a qualified
non-preemptible pool for the no-planned-preemption customer path; native
checkpoint recovery, not GPU-process snapshotting, is the fallback for node
loss. Future resumes near artifact-retention expiry need an explicit source
retention check; a fresh 14-day run is not a promise to retain old inputs forever.
