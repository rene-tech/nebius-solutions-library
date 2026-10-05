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
