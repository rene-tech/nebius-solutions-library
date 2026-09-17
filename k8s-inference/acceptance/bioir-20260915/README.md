# BioIR evaluation — 2026-09-15

Status: **all twelve model evaluations and four snapshot-model studies measured**.
No candidate has been promoted to production. Start with the
[consolidated report](report/report.md) for measured gains, quality regressions
and recommendations; the lane reports preserve the detailed evidence.

Compare BioNeMo Inference Runtime with the exact current Scientific AI serving
implementations using otherwise unused H100/L40S GPUs. Production stays unchanged.

Canonical management record:
`/home/tux/dashboard/data/epics/nim-fast-start-platform/tasks/fs2-bioir-manager-r20260915/task.md`.
Shared experiment contract:
`/home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md`.

## Required coverage

| Model | Owner | Expected BIR fit (hypothesis, not a result) |
| --- | --- | --- |
| boltz2 | boltz2 | Supported structure-prediction pipeline; affinity is separate |
| openfold2 | openfold | Supported; exact checkpoint/configuration must match |
| openfold3 | openfold | Supported family; validate Preview2 checkpoint |
| diffdock | coverage | No documented end-to-end pipeline |
| evo2-40b | coverage | No documented end-to-end pipeline |
| genmol | coverage | No documented end-to-end pipeline |
| molmim | coverage | No documented end-to-end pipeline |
| msa-search-pdb70 | coverage | Database search not accelerated by BIR |
| proteinmpnn | coverage | No documented end-to-end pipeline |
| rfdiffusion | coverage | No documented end-to-end pipeline |
| proteina-complexa | coverage | Investigate matching module reuse, not a drop-in claim |
| protenix-v2 | protenix | Supplementary scope: supported module, no complete pipeline |

Related scientific models receive separately labelled applicability notes; they
are not silently rebranded BioNeMo or replaced by another model.

## Evidence format

Raw request bodies, predictions and logs are committed as byte-preserving
archives to keep thousands of generated files out of the source tree. Every
archive and every member has a SHA256 in
[evidence-archives/manifest.json](evidence-archives/manifest.json).
From this directory, run the following before opening raw-evidence links or
re-running offline analyses (Python 3.9+; standard library only):

```sh
python3 report/evidence_archives.py verify
python3 report/evidence_archives.py unpack
```

Unpacking restores the original paths and refuses to overwrite changed local
evidence. It does not contact the cluster or execute a benchmark. Reports,
summaries, manifests and benchmark code remain ordinary readable source files.
GPU checkpoint payloads and model weights are not included; their identities,
capture receipts and test results are retained.

Each lane retains its own raw artifacts, commands/manifests and human report.
Each lane should publish `result.json` with a top-level `models` array. Each
model record should contain `model_id`, `bir_fit`, `status`, `baseline`,
`comparisons`, `quality`, `features`, `snapshot`, `recommendation`, `limitations`
and `evidence`. Nested fields can retain lane-specific detail. Unmeasured
quantities must be null/absent, never zero. Include hardware, exact release
identities and sample counts with every timing. No secret/customer data.

Status values: `measured`, `partial`, `unsupported`, `blocked`, `pending`.
An unsupported BIR candidate still needs a fresh baseline or explicit baseline
blocker. An existing historical benchmark is not a fresh baseline. Baseline
and candidate on different GPUs are not a valid speedup pair.

## Report acceptance

- All twelve rows accounted for, including failed and unsupported cases.
- Distinguish execution acceleration from persistent-model residency savings.
- End-to-end request latency, valid-request throughput and allocated GPU-seconds
  per successful request, not only a timed forward pass.
- Cold/cache/snapshot cohorts and feature/quality regressions explicit.
- Numeric speedups derived from comparable retained data only.
- Recommendations and proposed deployment work are separate from this study;
  no candidate is promoted automatically.

The current root checkout contains unrelated ongoing work. This evaluation uses
one isolated shared worktree; only the manager integrates commits.
