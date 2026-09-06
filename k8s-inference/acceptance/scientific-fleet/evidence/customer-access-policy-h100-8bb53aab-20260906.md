# Live customer access and scientific policy acceptance

**Passed on deployed source `8bb53aab`, 2026-09-06.** The
[machine-readable evidence](customer-access-policy-h100-8bb53aab-20260906.json)
records exact deployment, runtime, artifact and receipt identities. The cluster
was left running with its original effective model policy restored.

## Customer access

The real admin browser created a key for a temporary academic viewer, scoped
to ESMFold2-Fast, AlphaFold3 and BindCraft. Public scientific discovery returned
exactly those three models; AlphaFold3 and BindCraft retained verified academic
access. Interactive discovery returned no models. An out-of-scope RFdiffusion
submission returned 403 before admission. Browser revocation was followed by a
public 401, and the temporary principal was disabled. No customer credential is
included in the evidence, and the access probe submitted no GPU work.

This closes the scientific-key 404 failure documented in the
[initial access review](customer-access-20260906.md). License-restricted
inference qualification remains the separate fleet's responsibility; discovery
alone is not claimed as model computation acceptance.

## Pause, resume and active-run concurrency

Only ESMFold2-Fast was changed, through the actual Scientific runs UI. Its
original policy had no row: unpaused and uncapped. Revision 1 paused dispatch;
two independent requests were accepted and remained durable with zero attempts
and no matching Kubernetes Pods/Jobs. There were 24 observed samples with both
requests held. Revision 2 resumed with a cap of one; the UI correctly showed
`at-limit`, one running and one queued.

Both requests then succeeded automatically, with idempotent replay, semantic
result validation, and SHA256/size-checked downloads of each output manifest
and scientific JSON artifact. The second dispatch began 81.606 ms after the first
operation completed. Across 274 read-only samples, the maximum running-batch
count was one, and the final receipt showed one overlapping GPU attempt at
most. Scenario names do not imply dispatch order: they were submitted
concurrently. This test proves serialization, not a live priority-preemption
or tie-breaking test; those concurrency/priority checks also have real
PostgreSQL coverage in the policy implementation.

Revision 3 restored unpaused, uncapped, empty reason through the browser at
22:50:07 UTC, with independent API readback at 22:50:09. The retained revision
is intentional; effective behavior matches the original absent row. Only then
was the separately managed fleet's ESMFold2-Fast worker released. The other
nine fleet models were allowed to run concurrently throughout.

## Timing and optimization findings

| Dispatch order | Harness wall time including deliberate holds | Server execution | GPU occupied / active / idle |
| --- | ---: | ---: | ---: |
| First | 320.244s | 265.173s | 64 / 27 / 37 GPU-s |
| Second | 592.587s | 268.930s | 65 / 28 / 37 GPU-s |

These wall times are **not isolated cold-start benchmarks**: they include the
deliberate pause, and the second includes the first request's execution. Both
CPU preparation Pods landed on different CPU nodes with cold image caches,
each pulling the 3.816 GB model image (138.167 s and 148.679 s). GPU artifact
verification took 30 s and 29 s; the model processes took 27 s and 28 s. These are
separate clocks, not sums that should be confused with end-to-end latency.

Both folds ran on existing reserved H100 capacity. GPU accounting reconciled
with no gaps and is application-observed, not sampled kernel utilization.
No GPU snapshot was used, and no dedicated model-load/warmup measurement was
invented. Heavy CPU-stage image locality and repeated GPU artifact checks
remain concrete optimization opportunities; no resource caps or deployment
configuration were changed during this acceptance.

## Cleanup and scope

Temporary access was revoked, the principal disabled, the original effective
policy restored, and both accepted runs finished. No matching Pods, Jobs or
Kueue Workloads remained; the observer's admin session and Pod watch ended.
Other models and their policies were not modified. Real browser screenshots,
full operation receipts, Pod events and the 274-sample policy timeline are
retained privately by the acceptance task; their public summary carries the
immutable hashes.
