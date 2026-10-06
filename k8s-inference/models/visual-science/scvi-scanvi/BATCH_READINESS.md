# scVI/scANVI large-data PoC implementation

Task: `fs2-scvi-scanvi-whitelab-readiness-r20261006` (Agent Task Deck).
Source starts at `19bfdf2c3`, the verified live shared-backend lineage.

## Status

**Candidate, not customer-ready and not published.** The existing native App is
unchanged. A 500k-cell routine target and a 1M-cell atlas target are qualification
goals, not measured capacity. WhiteLab has not supplied representative data yet.

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

## Tests and next gates

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
- Remaining: exact GPU results; durable-companion checkpoint recovery; larger
  profile/catalog/schema and execution-map projection; failure diagnostics;
  large-file REST/MCP transfer; API/MCP queue/replay/cancel/scaling; scientific
  holdout/bioconservation metrics; 1M-cell public data; final promotion/evidence.

Do not count synthetic interruption fixtures as biological or atlas qualification.
Do not publish a route or change shared images based on these component tests.

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
