# Lynx long-run GROMACS and idle capacity — 2026-10-05

Owner-requested changes: fourteen-day GROMACS execution budgets, the existing
Lynx shared bucket at 100 GB, meaningful late-stage continuation tests, matched
L40S/H100/multi-GPU optimization, and idle Apps without hot replica floors.

This is an in-progress integration record, **not a completed release claim**.
Parent task: `fs2-lynx-gromacs-longrun-performance-r20261005` in the NIM Fast
Start Platform epic. Source branch: `agent/fs2-lynx-longrun-performance-20261005`,
based on `7d0f6e0ba` (the deployed continuation incident fixes).

## Storage change completed

At 15:05:57 UTC, the existing supported operator storage API and normal
reconciler changed `lynx` shared storage from 5,000,000,000 to
**100,000,000,000 bytes**. This is decimal 100 GB, not 100 GiB.

- Existing bucket: `fs2-lynx-c327dcc386444425`.
- Provider ID: `storagebucket-e007042062916934702682`.
- Project/region: `project-e00rene`, `eu-north1`.
- The only changed provider spec field was `max_size_bytes`.
- Tenant mode, bucket ID/name, endpoint, region, ready status and access key
  identity were verified unchanged. No keys rotated or customer data deleted.
- No inference key or S3 secret belonging to Lynx was loaded. No test objects
  were written into their bucket. No project/cloud quota was changed.
- Private before/after/plan/verification receipts:
  `/home/tux/secure-handoff/fs2-lynx-longrun-20261005/storage-apply/`.

`resize_storage.py` is a reproducible exact-target operator helper; preview is
the default and `--apply` uses the existing admin policy endpoint. The provider
is read-only in this helper; the platform remains the lifecycle owner.

## Preserve the customer's running job

Lynx operation `a42479f9-5ee0-4ed4-869b-0a094357403f` is left running with its
original immutable execution plan. Its native budget was frozen at seven days
when it started. Patching a Kubernetes deadline alone would not extend the
already-running engine wrapper. New jobs and explicit continuations will use
fourteen days after the coherent release; no fourteen-day soak is claimed.

At 15:06 UTC, its existing pod had both containers ready, zero restarts and a
committed generation-10 checkpoint in the same customer bucket. New test work
uses existing `system/qa` or `system/development`, never a customer key.

## Parallel implementation and acceptance

- `fs2-gromacs-fourteen-day-late-restarts-r20261005`: compatible reader/writer
  and worker limits, file-backed stage descriptors, bounded compressed durable
  metadata, large-inventory real native REST/MCP continuations and replay.
- `fs2-idle-model-hot-floor-removal-r20261005`: supported admin min-replica
  revisions, existing-controller adoption for legacy static Apps, wake/drain
  tests, no removal of active work.
- `fs2-lynx-gromacs-gpu-optimization-r20261005`: exact private Lynx TPR copied
  read-only to internal qualification storage, unchanged scientific protocol,
  matched GPU/offload/layout measurements, public REST/MCP results and costs.

Initial replica-floor changes released the full H100 nodes for the performance
cohort. Native performance probes alone do not qualify the hosted API or client.
Each subtask retains failed attempts and final exact release identities.

## Checkpoint I/O changes under test

Repeated hashing and S3 HEAD requests for all earlier trajectory segments made
checkpoint cost grow with the complete history. The candidate keeps a bounded
process-local stat-signature/hash cache and verified content-addressed export
cache. Caches are cold on process restart; changed files are hashed again.
Fresh files are not memoized within filesystem timestamp granularity. Each
completed native step rechecks remote objects; a missing customer copy is
restored from the verified workspace. Customer deletion of exported objects
during an active step cannot be prevented; the independent platform checkpoint
remains the restore authority. No native files or debug logs are discarded.

The native workspace bound is 32,766 files (with separate manifest slots).
Twenty thousand small synthetic history files exercise metadata scale while
real GROMACS checkpoint bytes exercise continuation; they must not be described
as fourteen days of physically simulated production data. Other byte/time and
admission bounds remain explicit.

Local initial regression: 58 storage and inventory-cache tests passed, including
20,000 files, source mutation, remote conflict/deletion, restart, checkpoint
manifest-last commit, and no repeat HEAD for immutable prior segments. These
tests are not a replacement for the live late-state acceptance.

## Long-run release deployed; larger live acceptance still running

Source API commit: `b9cc34f10af61eedf6548abac387a0133dea7902`.
Worker source commit: `66eeb1a8e` (the following collector/retention changes
do not alter native worker bytes). Exact regional image digests:

| Component | Digest |
| --- | --- |
| API, scientific tools and maintenance | `sha256:ec7ff6833b9e8ccd49cd6ff19e25308019b6bbd0bc1326c1becf83657eeace22` |
| GROMACS single-GPU worker | `sha256:5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd` |
| GROMACS MPI worker | `sha256:fc28fa44489a93a6c73ccb0852e3970025cda1dda65a97d430f8076f2d2a3a92` |

All are under the existing `e00akg9ndpx77eaexh/fs2-platform` repository in
`cr.eu-north1.nebius.cloud`. API/tools/maintenance use `fs2-serve-control-plane`;
both scientific workers use `gromacs`. The execution ConfigMap is
`fs2-r927c465c6d-scientific-execution-23a04755dc69`.

The single-GPU candidate passed three real 10,000-step membrane-benchmark runs
on each of H100 and L40S; the MPI candidate passed the same one-GPU H100
regression, including observed rank bindings. All retained output bytes were
hashed and native final coordinates/energies checked. These bounded wrapper
regressions do **not** substitute for the larger exact-Lynx performance and
large-history continuation cohorts in the child tasks.

The deployment first rolled a temporary compatible reader over the legacy
public API, then verified every old reader had exited. The final API/tools/map
were activated together, preserving the idle-model agent's latest live route,
envelope and bundle bindings. Three exact-image API replicas and authenticated
public discovery passed verification. No ingress or public-site routing changed.

Testing also found that generic payload cleanup could expire active scientific
API operations after the default 24-hour TTL. Active scientific outbox/batch
owners are now excluded during both candidate selection and the locked recheck.
Terminal scientific cleanup and ordinary inference TTL remain unchanged. A
real PostgreSQL test under the existing maintenance role passed after advancing
time beyond 15 days; no database grants changed. The scheduled maintenance
worker was updated too, and two owned Jobs on the new image completed at
15:42:18 and 15:43:16 UTC. No previous maintenance reader remained active.

At the final API verification, the original Lynx pod still had zero container
restarts and generation 18 committed. Its immutable seven-day native budget
is unchanged, as described above.

Private reproducible release evidence is under
`/home/tux/secure-handoff/fs2-lynx-longrun-20261005/release/`: source build
provenance/OCI archives, native receipts, bound source recipes, compatible-reader
and final-reader observations, server-dry-run/exact-template activation patches,
the narrow Helm overlay, and maintenance verification. The earlier local
`f12641f74` API build was superseded by the retention-fixed build and was never
activated. **Live 20,000-file REST/MCP continuation and final optimized-workload
results are still pending; there is no fourteen-day soak or combined-ready claim.**

## Large-history test exposed metadata round-trip cost

The first real 20,007-file checkpoint on internal operation
`682e77af-3d22-45db-9c3f-29b8515cde70` exceeded the existing 600-second handoff
timeout. Generation zero remained committed: an incomplete upload was **not**
acknowledged as recoverable. Failure evidence remains in the restart subtask.

Read-only PostgreSQL evidence identified the slow phase before customer S3
export: 12,032 finalized objects, only 3,846,298 bytes, between 15:53:07 and
16:04:01 UTC. Each 64-file HTTP cohort still performed per-file database
transactions. Representative cohorts spent 0.4–1.9 seconds reserving metadata
and 0.5–2.7 seconds finalizing it; PUT plus the first finalization took
0.18–0.62 seconds. Merely extending the timeout would hide this bottleneck.

`scientific_artifact_batches.py` now uses bounded set-based reservation and
finalization under the existing runtime role, tables, identity constraints,
attempt/terminal fences and event ledger. Every new object is still independently
read and SHA-256 verified; customer checkpoint manifests are still published
last. Forty-five artifact tests passed including actual PostgreSQL replay,
ordering, conflict rollback, scope and superseded-attempt checks. A 2,048-file
SQL-only regression completed in 1.59 seconds locally; this is **not** a measured
end-to-end cluster transfer rate. Ninety-eight related route/storage/continuation
tests passed; thirteen optional external-object-store tests were skipped.

API/tools/maintenance successor `sha256:6f0703fa08f229a5f5b2e0239c5bfeb45d777e961a035483226d3f0f46fbe481`
(source `00fe3ca09`) was verified with three exact API readers, public discovery,
eight MPI shapes and successful maintenance Jobs. It preserves the native worker
images. The running Lynx Pod retained the same UID and zero container restarts.
Additional bounded read batches now cover large continuation admission, artifact
download handles and terminal stage commits; the next source is `d654cb1a3`.
Its actual PostgreSQL test covered 20,000 unique files and concurrent/replayed
20,001-entry final manifests. The successor `979e85fa4bacb8e1b72063fb2bf300834d9a9e6c90045c346e1565d33bb171ba`
was verified at 16:33 UTC: three exact API readers, public discovery and the
scheduled maintenance worker. It preserves the native engine images.

The repeated source operation `33b398b6-4311-4400-9a0b-67dfa2e596b5` committed
all 20,007 files and its 8.324 MB manifest at 16:39:38 UTC. Platform publication
took 332.78 seconds; customer export brought total handoff to about 490 seconds,
inside the unchanged 600-second limit. The intentionally short native run stopped
at step 7,800/60,000 with the expected workflow time-limit outcome, not a transport
timeout. This qualifies checkpoint publication, not yet completed continuation.

Its explicit continuation `5dfc4c25-8747-403f-8ec6-07525a1ade98` was admitted and
is running. Initial HTTP delivery failed while admission took 65.8 seconds;
read-only operation/idempotency checks prevented duplicate work. This latency
defect remains an acceptance failure until fixed and retested. Final-attempt
publication after same-operation recovery was also changed to bounded batches
(`3dd88de96`), with 70 targeted tests, and built from bound source `8f70f3025`.
That image is not yet an accepted recovery release.

The added NVIDIA MPS/MIG article is tracked in
`../lynx-mps-20261005/README.md` and its linked Task Deck child. Isolated H100 and
L40S comparisons run in parallel. No customer sharing mode or MIG geometry is
being enabled from a native performance screen.
