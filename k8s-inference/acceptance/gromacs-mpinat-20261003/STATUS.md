# MPINAT campaign — partial, storage resolved and runs resumed (2026-10-03)

This is **not** a completed REST/agent/MPI qualification. Never use the successful
subset to describe the whole suite or a sixteen-GPU MPI execution as working.

## Results retained

| Cohort | Outcome | Interpretation |
| --- | --- | --- |
| REST-A | 4 successes, 12 native failures | Upstream short-run `-resethway` aborted while PME tuning was active. Failed attempts and original native diagnostics are retained. |
| REST-B, all 24 inputs | 20 verified successes, 3 failures, 1 explicitly cancelled | Only the forced timer reset was removed. Three large jobs encountered the full QA bucket; the remaining large run was cancelled because delivery could not succeed. |
| Actual seeded agent/skill/MCP | Incomplete | Exact customer client image, existing system/QA key, isolated local instance. Includes an erroneous agent re-submission, a qualification-login expiry, and durable studies that have not all completed. Not Serverless/browser qualification. |
| MPI / strong scaling | Not run in this campaign | Existing API shape is 2–8 nodes with one GPU per node and TCP MPI; full-node GPU layouts and sixteen-GPU MPI still need implementation/qualification. |

REST-B reached **12 simultaneously GPU-claiming running Pods**, with up to **8
GROMACS GPU processes observed in the sampled device checks**. Admission limit
sixteen is not evidence of sixteen GPUs computing concurrently. All 258 catalog
availability probes returned HTTP 200; this alone is not whole-platform health
or customer-experience qualification.

Each successful REST case has three timing repetitions of 10,000 steps. Timings
include initialization and warmup; these are not independent scientific replicas
or post-tuning steady-state measurements. Fixed-lambda inputs do not establish
free-energy convergence.

## Durable evidence and measurement limits

Private evidence root:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003`.
It contains immutable input provenance, `rest-a`, `rest-b`, `agent-a/b/c`,
`bucket-a/b`, `telemetry`, `measurements.sqlite` and exact release receipts.
Credentials are not committed. The corresponding Task Deck card is
`fs2-gromacs-mpinat-api-mcp-mpi-r20261003`.

See [MEASUREMENTS.md](MEASUREMENTS.md) for the per-operation/replica/phase/attempt
contract and quality labels. Raw data remains available; the SQLite index is
rebuildable, and platform PostgreSQL remains authoritative. The current REST
index contains forty operations/attempts; agent traces must still be reconciled
to that index before cross-interface comparisons are claimed complete.

Exact device release, native initialization versus integration, some transfer
subspans, and resumed-execution step attribution are **not all instrumented**.
Unknowns remain null. Failed logs are not certified useful progress, and retries
must be unioned by logical replica rather than charged twice. No billing-grade
completeness claim is made.

## Fixes delivered during this campaign

- Native failure diagnostics reuse committed content-addressed artifacts with
  their original MIME type. Changed bytes get a new digest without advancing a
  checkpoint generation or pretending a failed simulation succeeded.
- Collector source commit: `35964c293e15b8110158421a3b208b4842ac98f6`.
  Deployed image:
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:e880bc5b9f2dae293ca0fe591b1f1c4bad31d55e7185bb06da355c0205d69109`.
  Only `FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE` was changed on the captured live
  deployment. API and native GROMACS images were not replaced. All three API
  replicas rolled out. Exact inverse patch and Helm reconciliation overlay are
  retained under `release-v2/activation`; do not replay stale full Helm values.
- Live expected-failure operation `d4e9e8f0-43f4-48a7-9389-a0bb693fc5f3`
  ran with the new collector digest. Its failed result contains downloadable,
  hash-verified native diagnostics naming `deliberately-missing.xtc`. This test
  used **platform artifacts**, not the full customer bucket. It verifies failure
  reporting, not successful MD or customer-bucket export.
- Qualification login renewal was corrected in client commit `ffafc2a`, pushed
  to `rene-tech/serverless-ai-cookbook` main. No customer image or login changed.
- REST runner now keeps polling after a terminal operation status until result
  publication. `--collect-only` recovers existing terminal artifacts without new
  submissions, resource allocation, cancellation or key-policy changes.

## Storage interruption and resumption

The provider rejected writes to `fs2-system-35f1ad07f2fe32cf` with
`BucketMaxSizeExceeded` (HTTP 400), at its existing 5 GB policy limit. No bucket
or project quota was raised, and no objects were deleted. Owner approval was
requested to raise only this internal QA bucket to 100 GB; one blocker DM was
sent, not repeated progress messages. The owner subsequently approved the
increase. The existing tenant-storage API/reconciler raised only this shared
system bucket to **100,000,000,000 bytes**; provider identity/spec comparison and
S3 write/read verification passed. Receipt: `storage-resize-approved/verification.json`.
Bucket ID remains `storagebucket-e008997264638438027797`. No other bucket policy,
project quota, credentials or stored objects changed.

At the pause, all benchmark GPU Pods drained. QA concurrency was restored to **2** with
no expiration change. The replay process and task-owned local client
`fs2-default-release-mpinat-20261003` are stopped; its container filesystem and
bind-mounted workspace remain intact. The telemetry observer is stopped using
its STOP marker. Customer keys, instances and workloads were not changed.

After approval, a separate `rest-c` cohort restarts the four unfinished large
cases with concurrency four; it will restore QA concurrency two on drain.
New raw telemetry uses `telemetry-c`, preserving the stopped earlier observer.

## Resume work

1. Storage approval and resize verification are complete. Continue monitoring
   retained bytes/headroom; do not silently delete old data or raise other limits.
2. Inventory the retained agent studies before restarting that client: queued
   studies may resume automatically. Poll existing operation IDs first; never
   duplicate an uncertain submission under a new idempotency key.
3. Run the remaining large cases with distinct, documented attempts/cohorts and
   unchanged physics. Preserve failed and cancelled ranges. Re-verify customer
   bucket export and original failure diagnostics on that path.
4. Complete all 24 actual agent/skill/MCP cases, resolve the erroneous retry and
   study/observation behavior, and reconcile agent operations into the ledger.
5. Implement/qualify multi-GPU resource shapes and MPI on available capacity.
   Keep healthy-but-busy full-node pool queue delays visible; the existing
   dead-pool recovery test does not qualify this different case.
6. Close measurement gaps before claiming complete phase accounting; report
   coverage and uncertainty with timings, resource occupancy and waste.

No customer key is permitted for these internal tests.
