# MPINAT campaign — partial, MPI implementation and qualification (2026-10-03)

This is **not** a completed REST/agent/MPI qualification. Never use the successful
subset to describe the whole suite or a sixteen-GPU MPI execution as working.

## Latest checkpoint — 13:15 UTC

Six native candidate checks passed: single H100/L40S, MPI1x1H100,
MPI1x2/1x4L40S, and legacy2x1H100. All task-owned native Pods/JobSet were
released. Final coordinates/cells, energies, checkpoint steps, inventory hashes
and rank bindings were verified. These direct-Pod checks are not REST/MCP
qualification or steady-state performance comparisons. Exact summary is
`runtime-qualification-r1/summary-01.json`, SHA256
`949e92e4c9770ed2b372a6ef802fb88c7ed879f9a81134e66d8a4e1835898cb7`.

Reader-first rollout is complete on all three production API replicas:
`fs2-serve-control-plane@sha256:e695551a804f0f2f9b7615c4d8f60282e00861b31f6d5f20afaf97b7bd06dac4`.
It contains new code with the exact old catalog, so it still publishes zero
new execution shapes. Old API Pods have exited; authenticated public discovery
returns200. This avoids mixed-version readers rejecting durable shaped records.
The final `d789a4b7e3d8a95ca296f25e1498714729def18b70de6a91f63cf70d5aafed68`
image and narrowly scoped execution/scheduling changes passed server dry-run;
activation awaits the remaining cross-namespace reader-isolation check.
No Gateway, customer placement, credentials or limits were changed.

Actual seeded-agent recovery now produced five valid CSVs with15 native timing
rows, without new GPU submissions. One delivery failed because recovery receipts
had a different filename; the bridge fix is in exact client candidate
`lc@sha256:74b578936eb911320e1ddcec733fa9b26c7c9575b4aee586f977c2b0f3d5d1db`,
source`59789c88d53a5270c7463181f169975b642fb29d`. Exact-image delivery retest is
running in isolated QA; no customer client/default has changed. Native matched
CPU/GPU-PME A/B probes are running in parallel on spare L40S capacity.

The approved100GB QA bucket currently contains about10.14GB by provider counters.
The statements below describe the preceding12:50 checkpoint where superseded.

## Previous checkpoint — 12:50 UTC

REST-C is drained: MEM and RIB verified, both PEP variants failed their frozen
4 GiB output budget. The largest coordinate files are about862MB each; three
repeats plus segment/final copies do not fit. Corrected requests use24GiB inside
the existing48GiB API maximum. Preserve original failed attempts; this does not
raise provider quotas. New runtime publishes exact inventory failure plus bounded
native diagnostics instead of losing `result.json` to the inventory exception.

The independent-MPI runtime and per-Pod GPU/resource shapes are implemented;
backend tests:311 including actual PostgreSQL durable reopening. Runtime source
`0aecab6bdca3346a74426cea24a4753cd4f54270`. Regional candidate images are
`gromacs@sha256:dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93`
(single) and
`gromacs@sha256:c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea`
(MPI). Neither is activated yet; spare-GPU runtime qualification is ongoing.

Saved agent studies now finish their native operations after the check-once MCP
timeout fix. Their timing analysis incorrectly read the platform envelope instead
of the native result; empty tables are not benchmark passes. Candidate client
adds explicit native-result references and updated skill, preserving the current
default image features. No customer endpoint has changed.

Query ledger:44 REST operations/attempts,303 checkpoints,95 native mdrun records.
`cost-analysis-01/report-04.json` SHA256
`66ee981271623ea1e2bca2bc2c349b9e1d0446b2a7cc6d1b6ae7eeb6e81d7176`
contains per-operation/attempt costs, useful progress, allocation bounds and
historical comparisons. No saved REST operation is missing from this index.
Source code is `cost_report.py`; references and tests are adjacent.

The current deployed collector also passed customer-bucket failure reporting:
operation`4c27cdf3-3090-4e63-835e-dd649a3c431b` intentionally fails on a missing
XTC. Both platform diagnostics and2 remote checkpoint manifests/3 text files
were retrieved and hash-verified (`negative-v3`, `negative-v3-bucket`). This is
an expected failure-path pass, not a successful simulation.

Two full H100 nodes have NVLink but no RDMA network attachment. Other Apps occupy
15/16 GPUs. No customer Apps have moved; rolling relocation was asked about once.
8/16GPU performance remains unmeasured until actual whole-node capacity is free.
The following tables/history predate this checkpoint where explicitly noted.

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
