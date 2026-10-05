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

Its explicit continuation `5dfc4c25-8747-403f-8ec6-07525a1ade98` was admitted.
Initial HTTP delivery failed while admission took 65.8 seconds;
read-only operation/idempotency checks prevented duplicate work. This latency
defect remains an acceptance failure until fixed and retested. Final-attempt
publication after same-operation recovery was also changed to bounded batches
(`3dd88de96`), with 70 targeted tests, and built from bound source `8f70f3025`.
That image was deployed as `06e2edce49dd2c432142d5975daf2b4b0f5aa5efa31abd9ea89add8a6c7248ba`,
with exact three-reader/public/maintenance verification. It did not itself
qualify a successful large-history continuation.

The continuation later failed the unchanged 600-second checkpoint handoff
limit: only 8,256 of 20,011 files were published after 604.6 seconds. Input
restore had taken 333 seconds. Repeated decoding of the same immutable 20,000-
file execution plan on metadata requests caused substantial avoidable CPU
work; the valid source checkpoint remained available. Resource release was
observed, and this failed attempt remains part of the acceptance evidence.

Source `046337b19` replaced quadratic materialization lookups with a linear
index and cooperative scheduling. Source `d3d670475` added an explicitly
bounded cache only for verified immutable compressed scientific documents:
four entries, at most 64 MiB of retained Python object graphs. Live attempt,
cancellation, identity and access checks still read current state. Real
20,000-file tests verified cancellation/supersession fences and malformed or
mutated document rejection; this is not a credentials or authorization cache.

The bound release source `8a4123ac61cc2bbcc2c09934bae248e03a2c2df9` produced
API/tools/maintenance image
`ea48e96625af79e91a2ebf8fda2403207c637ba3cb35cc0328c1d08a0bdfa622`.
Execution ConfigMap: `fs2-r927c465c6d-scientific-execution-5802297e094c`.
All three exact readers, no older/terminating readers, public discovery HTTP
200 and eight MPI shapes were verified. Scheduled maintenance Jobs completed
on this image at 17:05:16 and 17:06:16 UTC. Native worker images and the latest
idle-model envelope/bundle/route bindings were preserved.

REST continuation r3 (`59ae91d5-be0b-4e29-a7b6-151c7b7a4631`) ran from
the same safe source, with a new idempotency key. Initial admission and replay
returned valid 202 responses in 29.280 and 15.029 seconds, respectively, and
reused one operation. This is an improvement over the gateway failure, but
admission was still slow. Capture across all readers found four readiness 503
responses, not just the first observed failure. Input restore improved from
333 to 132 seconds, but publication of all 20,011 platform files took 599.69
seconds, leaving no time for customer export inside the unchanged 600-second
handoff deadline. The operation failed, its result was published, resources
were released, and the source checkpoint remained safe. This is retained as an
acceptance failure; successful artifact upload alone does not qualify restart.

The successor isolates pure metadata computation from the API event loop in a
bounded two-worker executor. Raw/repeated task cancellation drains a running
worker before releasing its database/lock scope. Set-based admission preserves
transaction ordering and replay checks. An additional bounded memo retains only
derived immutable artifact bindings; current tenant/key access, attempt,
cancellation and ownership are never memoized. Actual PostgreSQL, heartbeat,
replay, cancellation and immutable-binding mutation tests passed. The exact
source is `e718ccb71900c9dd9aaa7f2c56e77b8bd1a0574a`, image
`64c5c77d4eb6cac4b4ecf58d16766b2aa0423f836c1b9adee5a2034abd3e3644`.
This image is deployed with execution ConfigMap
`fs2-r927c465c6d-scientific-execution-64d5416c8f6d`. All three exact readers,
absence of old/terminating readers, eight MPI shapes and authenticated public
discovery HTTP 200 passed. Maintenance Jobs completed at 17:40:16 and 17:41:16
UTC. The latest idle envelope/bundles/routes and native workers were preserved.
Large REST/MCP completion, peer-loss recovery, concurrent ordinary requests
and the legacy seven-day-source continuation still require live acceptance.

The added NVIDIA MPS/MIG article is tracked in
`../lynx-mps-20261005/README.md` and its linked Task Deck child. Isolated H100 and
L40S comparisons completed 84 native trajectories with all native outputs
verified. Aggregate MPS throughput improved, but individual trajectory latency
did not. No customer sharing mode or MIG geometry was enabled from the screen.

## Authorized H100 InfiniBand reprovision — hardware complete

The user explicitly authorized reprovisioning the two full H100 nodes with a
GPU cluster and InfiniBand. The child task is
`fs2-h100-infiniband-reprovision-r20261005`. Only pool `h100-reserved-8x`, old
node group `mk8snodegroup-e00zswm0km7v78tp2m`, is in scope. Preserve its existing
16-GPU capacity block `capacityblockgroup-e00hfp6svlho8hsoywvzx`, STRICT
allocation and the compatible `eu-north1/fabric-2` placement. No quota change,
new reservation or unrelated node-pool modification is authorized here.

The two old nodes had no GPU-cluster attachment and no exposed RDMA capacity.
Both compute-instance and managed-node-group GPU-cluster membership are
creation-time settings; changing MPI flags cannot create InfiniBand hardware.
Their completed TCP baseline remains useful: the latest 2×8 public REST run
delivered native 58.008 ns/day, slower than the single-node results. New MPI
admissions are held during replacement; Lynx's running L40S job is excluded.

The existing infrastructure state/backend must remain authoritative. Initial
planning exposed stale unrelated inputs and a provider/state schema mismatch;
the broad plan was rejected without mutation. Only a reviewed explicit
replacement plan may be applied. Provider-managed H100 images must not acquire
a second competing MOFED/network-operator stack. Real device, cross-node RDMA
and public-workflow checks are required before claiming 16-GPU acceleration.

The approved targeted replacement completed using provider `0.5.276` and the
original Terraform state. New group `mk8snodegroup-e00twzfv2vh6gs8j4p` belongs to
`computegpucluster-e00p8hjysxfyk1n58x` on `fabric-2`. Both new nodes were Ready
at 17:28:36 UTC, with eight allocatable H100s and eight active 400 Gb/s
InfiniBand ports each. Shared filesystems and all other pools were preserved;
the original Lynx L40S Pod UID and zero restarts were unchanged. The provider
selected node patch `1.35.7-nebius-node.75` under the existing `1.35` setting;
CUDA 13.0/driver 580.173.02 remain unchanged. This patch change is not hidden.

A separate reviewed refresh-only Terraform plan updated outputs to the new
group ID/topology and retained the existing L40S-4x output, with zero cloud
resource actions. The provider image supplies the RDMA drivers but no Kubernetes
RDMA allocator. A scoped, driver-free device plugin and explicitly qualified
RDMA execution shape are being added; hardware readiness is not yet a public
16-GPU workflow or performance qualification. Private plans, hashes and live
receipts: `/home/tux/secure-handoff/fs2-h100-infiniband-reprovision-20261005/`.

## 18:14 UTC integration checkpoint — not finished

REST restart r4, operation `d2e5befa-367f-453d-94f4-cee8bca045bb`, completed
native GROMACS from checkpoint step 7,800 to the requested final step 60,000.
It released its GPU and removed its owned Pod. The first large handoff took
544.692 seconds, including the customer export, within the unchanged 600-second
bound; later incremental generations took 5.644 and 6.468 seconds. Verification
of all 20,031 final artifact bytes is still running. This is not yet a complete
restart acceptance: the cohort recorded four readiness 503s, one with the
explicit `database readiness check timed out` response.

Legacy seven-day-source continuation `5f710396-0dc8-4ea8-b3d1-fa0c24c87f85`
passed on the fourteen-day contract: native step 22,400 to 60,000, 29 final
files verified by size and SHA-256, eight previous immutable files preserved,
and idempotent replay reused the operation. This tests compatibility, not a
fourteen-day soak. The customer's running seven-day operation is unchanged.

Read-only diagnosis found the claim query repeatedly scanning/decompressing
terminal scientific state, including megabyte-sized checkpoint histories.
Migration `0038_scientific_claimable_index.sql` adds a partial index for exactly
the existing claim predicate; claim ordering, authorization, leases and fencing
are unchanged. Real PostgreSQL planner, eligibility, idempotent prebuild and
fencing tests passed. The existing migration-owner identity prebuilt this exact
index with `CREATE INDEX CONCURRENTLY` at 18:03:58–18:03:59 UTC (1.742 seconds).
The index is valid/ready and the unchanged claim query selects it. This online
step deliberately leaves the migration ledger at 37 until the coordinated
reader release. Receipts: `claim-index-verified/` under the private parent root.

A subsequent resource sample fell from approximately 4,001m to 1,310m CPU, but
this is not a controlled attribution or an availability pass. Raw MCP restart
`c9d6195b-8b69-4296-b1a3-c5596083f050` still took 30.423 seconds to admit and
37.100 seconds to replay; both returned the same operation. Readiness failures
continued during admission. A pre-armed read-only observer captured nine client
backends simultaneously sending the scientific-admission-outbox payload query
to API readers; these were not nine PostgreSQL parallel workers. Its CPU sample
was 1,455m. The source still synchronously decoded these outbox lists, and each
worker could fetch the same payload. That receive/recovery path is the next
bounded fix, not a reason to relax readiness or handoff timeouts.

The separate historical-metrics repair is source-tested in `0a5532db9`:
fresh queue reads remain at the existing scrape cadence; historical accounting
refreshes asynchronously at most every 30 seconds with a three-second budget,
explicit age/unavailability and no fabricated zero values. It is not deployed
at this checkpoint. See `../metrics-ledger-scrape-20261005/README.md`.

The scoped RDMA plugin is now installed on the two replacement nodes, exposing
one whole-node HCA bundle per node. A 16-rank CUDA-buffer probe passed over
`rc_mlx5`, with unique GPU/PCI/HCA bindings and local CUDA IPC, without additional
workload capabilities or increased memlock. Qualification exposed and preserved
two real issues: UCX queue defaults exceeded the existing lockable-memory bound,
and singleton GROMACS version/analysis commands inherited a distributed MPI
environment. Queue sizing is corrected; singleton-local transport isolation is
being tested. No new public RDMA shape has been published yet.

At 18:13 UTC the original Lynx Pod retained UID
`28cba283-f42d-4fe7-aac7-010ba2222f1e`, both containers Ready and zero restarts.
All tests use the existing internal QA identity; no customer key or limit was
changed. Migration 38/all-reader activation, large MCP terminal validation,
peer-loss recovery and public RDMA performance remain open.

## 18:41 UTC integration checkpoint — schema38 deployed, acceptance still failing

The coordinated release completed at 18:30 UTC. API, startup schema readers,
model controller, future scientific-tools and scheduled maintenance now use
`sha256:191e2c2be4c131c78fd190d620f71dc2b6cfff285c21ab1af4acfb452684c195`,
built from `7a37a741c86330d25f74eff3fb4c3c6df9e3dabe`. The guarded migration
verified the prebuilt claim index and advanced the ledger from37 to38 at
18:30:10. Every old API reader exited before release verification. The eight
existing public MPI shapes, execution map and native worker images are unchanged;
no unqualified RDMA shape was published.

This release also bounds outbox recovery to one fleet owner and moves large
outbox decoding off the request event loop. The already-described historical
metrics repair is deployed: live queue counts remain fresh, historical samples
have explicit freshness/unavailability. Ten concurrent scrape cohorts across
all three readers passed, with at least two successful historical refreshes per
reader. Final scrape durations were0.282–0.443s. Two new scheduled maintenance
Jobs completed at18:35:17 and18:36:17. No new database permission was needed.

REST r4's full final artifact verification has now completed: all20,031 files,
23,042,709 bytes, verified by size and SHA-256. Its earlier readiness failures
still make it a failed overall availability test. Earlier raw MCP operation
`c9d6195b-8b69-4296-b1a3-c5596083f050` completed native work and published its
result at18:26; its Pod was absent by18:26:48. Full artifact verification is
continuing. Three extra GET attempts occurred during the compatible reader
rollout and are retained rather than reported as zero retries.

The new concurrent20k cohort uses191e throughout admission and future tools:
REST `7fb06cae-0eb6-44ab-8f5d-772d2fb3ae86` and raw MCP
`c2d128a9-80f5-418f-82a6-0372b4fdb7a7`. Initial/replayed admissions reused each
operation, taking33.882/30.778s and25.576/21.975s respectively. Complete pre-armed
body capture still found `database_unavailable` readiness503s during admission.
These are new acceptance failures, not a pass. The database observer showed
roughly478–494m CPU and no active application query at the sampled failure
times; the earlier database saturation is not sufficient to explain them.
A separate worker is diagnosing the remaining application/pool boundary while
both native continuations proceed without runtime modification.

On InfiniBand, the exact16-rank CUDA-buffer probe passes, but a byte-checked
9MB host broadcast fails with `ibv_reg_mr Cannot allocate memory` and the
existing8192kB memlock limit. The actual GROMACS attempt stopped before step0.
A separate TCP control and narrowly scoped RDMA-only IPC_LOCK executable
candidate are being tested. There is no native RDMA performance result yet.

Fresh sibling verification retained the original Lynx Pod UID, both containers
Ready, zero restarts, all31 managed App floors atzero, no idle App Pods, and the
unchanged workshop/admin images. The private authoritative root input now records
the deployed API/admin images, zero hot defaults and installed RDMA pool. No
whole-stack apply was run; the existing H200 ownership/input residual remains
documented in `../idle-scale-zero-20261005/PERSISTENCE.md`.

Private evidence: `schema38-stage-191e-r2.json`,
`schema38-restore-api-191e.json`, `schema38-restore-maintenance-191e.json`,
`release/outbox38-verification/`, `outbox38-metrics/`,
`outbox38-database-load.jsonl` and `concurrent-20k-r1/` under the private parent
root. No customer key, scientific input protocol, timeout or cloud quota changed.
