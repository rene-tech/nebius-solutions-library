# scVI/scANVI large-data PoC implementation

Task: `fs2-scvi-scanvi-whitelab-readiness-r20261006` (Agent Task Deck).
Source starts at `19bfdf2c3`, the verified live shared-backend lineage.

## Status

**Live onboarding release; final PoC qualification in progress.** The existing
native App is unchanged; the new durable batch protocol is published separately
under the same App ID. WhiteLab has not supplied representative data yet.

At 14:23 UTC on 2026-10-06:

- Worker R7 `063877787f8c...` passed 17 unit tests plus real H100 full-state
  restart and reference-mapping component checks.
- Backend R4 `8b1dc669af81...` is live with three ready API replicas. R3 hosted
  REST and MCP scANVI runs succeeded and all 40 output artifacts per run passed
  byte-count/SHA verification. These used worker R5, not R7.
- The real 5.87 GB input passed public multipart upload and final SHA verification,
  including resuming a client that was OOM-killed after uploading 13 parts.
- Hosted full 584,944-cell run `983a0cba-5c3d-463a-8f63-7a00a0b5b3de` is
  running on R7 with tenant-bucket export enabled. The separate one-million-cell
  direct runtime test uses R6; it is not a hosted-path qualification.
- The first hosted fault injection found worker interruptions exited 1; R7
  fixes this. The second found scheduler classification still made exit 75 a
  permanent application failure. The narrowly scoped correction passed 101
  tests; backend R5 is being promoted. Neither failed run counts as a pass.
- Generic MCP polling previously returned `result_available=false` after batch
  success. R4 fixes this using the durable batch publication state.
- No customer key, node group, cloud quota, hot floor or unrelated route changed.

Remaining gates: hosted replacement-worker recovery, default customer-bucket
readback, public query mapping, cancellation, unchanged-release clean cohorts,
final large-data receipts and documentation. Biological validation remains a
customer/study-specific activity, not a platform execution claim.

### Measured R5 runtime, 2026-10-06

Immutable worker: `sha256:eb2835095574d290a90d289fc2790e49d984b5bb4fa4911f74156d7f172d77e5`.
Preemptible H100, 8 CPU / 128 GiB envelope. Raw counts: 584,944 cells × 27,402
genes, 1,138,668,948 stored entries; 2,000 batch-aware HVGs. Automatic scVI budget
was 14 epochs, followed by 20 scANVI epochs. Timings: load/selection 76.05 s,
scVI 111.81 s, scANVI 405.98 s, export/sample UMAP 100.90 s. Worker elapsed
698.97 s includes hashing/checkpoint work; download is separate. Process peak
RSS 22,125,211,648 bytes; PyTorch peak allocated GPU memory 357,876,736 bytes
(not total device usage or a CUDA-context-inclusive measure).

This is execution/capacity evidence, **not** reproduced HLCA paper accuracy or
proof of convergence. Full-label annotation and reference mapping completed in
the synthetic component suite, including epoch-0 interruption and full-state
restore. 14 exact-image runtime unit tests pass. R3 failed RNG serialization;
R4 restored state but exposed a reference-path bug and missing periodic saves;
neither counts as a passing cohort. R2 was suspended, not successful.

Hosted input staging of the real 5,873,612,847-byte file returned S3
`400 ObjectTooLarge` through the old single-PUT handle. The deployed backend adds
resumable multipart transfers using the existing immutable artifact intent,
tenant authorization and final SHA-256 verification; no quota increase or
new identity store. This passed against real storage. Existing `system/qa` received **only** an additional `scvi-scanvi`
model grant; no credential rotation, expiry, concurrency or budget changes.

Secret-free runtime receipts are retained in
`/home/tux/secure-handoff/fs2-scvi-whitelab-20261006/runtime-r5/`;
raw public inputs and results are on the task-owned PVC. The 1M real-cell atlas
cohort uses a separate 128 GiB PVC and one GPU/256 GiB on an existing full H100
node because single-GPU hosts cannot provide that host-memory envelope. It
does not reserve eight GPUs or provision/resize any node group.

Focused tests: 130 passed / 4 explicit skips, then 105 public-artifact/batch/MD
regressions passed. A separate pre-existing native-MD publication fixture fails
on its qualification-baseline projection; reproduced unchanged on base commit
19bfdf2c3. It is not a new scVI regression and has not been hidden or marked passing.

The batch worker now supports counts from X, raw.X or a named layer, explicit
gene selection, separate scVI/scANVI budgets, early stopping, label probabilities,
reference export/query mapping, sampled or disabled visualization, and full
Lightning checkpoints (optimizer, scheduler, loops and RNG), not just saved
weights. No fixed cell-count or 20-epoch ceiling is imposed on this new lane.
Expanded input memory is estimated before loading, against the actual worker
envelope. It reads only the requested matrix and obs/var, not unrelated layers.

The existing scientific adapter/checkpoint transport is extended additively.
Queueing, ownership, immutable artifacts, idempotency, cancellation and usage
remain the platform's responsibility, not a second single-cell service.
Full input data stays separate from checkpoints. Resource admission is still
required to avoid one PoC request crashing another customer's work.

## Earlier development history (not current readiness)

R1 isolated H100 run (`b87f4644ae66`) failed before training because upstream
SaveCheckpoint constructs a filename from a non-null monitor. The custom full
checkpoint callback now supplies an explicit filename, covered by a new test.
The runtime cache directories also use task-owned writable paths instead of
image-inherited root-owned directories. R2 image is `45bae1378c76`.
Both attempts' input/output files remain on the same task-owned PVC in separate
`r1/` and `r2/` prefixes. No failed attempt is counted as passing.

Failure diagnostics now distinguish single-cell jobs from MD command jobs.
The expanded focused platform suite passes 82 tests (one pre-existing Starlette
deprecation warning), including a single-cell failure receipt without publishing
partial embeddings as success. MD-specific fixtures remain MD-specific; shared
binding/checkpoint tests also cover the new single-cell registration.

- Eleven in-image data/parameter tests pass (2026-10-06).
- 61 focused platform registry/input/checkpoint tests pass. Pytest reported
  cleanup warnings for unrelated pre-existing root-owned /tmp fixtures; these
  were not changed or deleted by this task.
- `qualification-job.yaml` is an isolated preemptible H100 job, not a public
  deployment. It first tests actual checkpoint recovery, full-label scANVI and
  query mapping, then the 584,944-cell HLCA core with raw counts, 2k HVGs,
  upstream automatic scVI epochs and 20 scANVI epochs.
- Its PVC is task-owned, expires for review on 2026-10-08, and must be exported
  to the existing system workspace before cleanup. No new tenant/bucket/key.
- This was the initial checklist. See the current status above for remaining gates.

Do not count synthetic interruption fixtures as biological or atlas qualification.
An active onboarding route is not a customer-readiness claim.

## Public evidence dataset

HLCA core CELLxGENE dataset `066943a2-fdac-4b29-b348-40cede398e4e`, CC BY 4.0,
Sikkema et al., Nature Medicine (2023), DOI `10.1038/s41591-023-02327-2`.
The 2026-10-06 asset has 584,944 cells and 5,873,612,847 bytes; preserve its
download hash and metadata. This release differs slightly in cell count from
the original paper's 584,444 core. Counts are in raw.X; X is processed.

## Resource profiles to qualify

Start with one H100 and 128 GiB host RAM for routine data; qualify a 256 GiB
profile on an existing full H100 node for larger matrices. 10/25 GiB input
targets require actual object transport tests and expanded-memory admission.
File size does not prove fit, and these envelopes do not prove convergence.

## Reproduce local data checks

Build from `k8s-inference/` using `models/visual-science/scvi-scanvi/Dockerfile.batch`.
Mount this directory read-only at `/tests`, override entrypoint to `python`, and
run `-m unittest discover -s /tests -p test_batch.py -v`.
The runtime image imports no GPU packages into the API: only its lightweight
parameters module is included in the control-plane wheel.
