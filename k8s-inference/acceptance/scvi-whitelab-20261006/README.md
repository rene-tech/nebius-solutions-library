# WhiteLab scVI/scANVI PoC qualification — 6 October 2026

**Deployed and tested for the scoped REST/MCP PoC below.** No WhiteLab data,
customer key or customer workload was used. This is not a declaration that
all platform models, biological results, or LibreChat agents are qualified.

Use the [customer instructions](../../models/visual-science/scvi-scanvi/README.md)
for submitting data, configuring training and collecting results.
[results.json](results.json) contains operation IDs, immutable input/runtime
identity, timings, resource measurements, attempts and validation receipts.

## Exact release and measured workload

- API: `https://89.169.99.188`; typed MCP: `/mcp`, `submit_scvi_scanvi`.
- Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`, `project-e00rene`, eu-north1.
- Backend R6: `sha256:f64cd4b39a6eaf41e1762837e0a878ee7a78307f973ef6a822a29b118b5510df`,
  source `96626104f8b04a8bf232371f1f43d35e07f3d5bd`.
- Worker R7: `sha256:063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246`.
- Pinned `scvi-tools 1.5.0.post1`, Torch 2.7.1+cu128, Lightning 2.6.5.
- Both large public-path cohorts used **one H100**, 8 CPU cores, 2,000 selected
  genes, batch size 512, a ten-dimensional latent representation, and 20 scANVI
  epochs. scVI used the explicit upstream automatic epoch heuristic.

| Cohort | Interface | scVI epochs | Worker execution | Accepted → completed | Peak host RSS | Verified output |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| HLCA core, 584,944 real cells | REST | 14 | 13.16 min | 15.11 min | 19.67 GiB | 42 files, 893,740,834 bytes |
| HLCA atlas subset, 1,000,000 real cells | MCP | 8 | 17.80 min | 21.28 min | 22.67 GiB | 42 files, 1,423,753,120 bytes |

Accepted-to-completed includes scheduling, staging and publication, **not the
client's preceding upload or subsequent download**. Worker time includes
preprocessing, training, checkpoints and export; it is not pure GPU compute.
These are individual runs, not latency percentiles or a performance SLA.
The routine run was admitted to `h100-ondemand-1x`; the atlas used one GPU in
`h100-reserved-8x`, with a 256 GiB host-memory envelope. Earlier isolated runtime
checks used available preemptible H100 capacity. No node group was resized.

Public data: [HLCA, Sikkema et al.](https://www.nature.com/articles/s41591-023-02327-2),
CC BY 4.0. The current core asset has 584,944 cells; the paper's original core
had 584,444. The atlas cohort selects the first one million **distinct** cells
from the larger CELLxGENE asset; it is not a representative biological sample
or duplicated rows. Raw-count selection, input SHA-256 and all parameters are
retained. The atlas required explicit `hvg_span: 1.0` after Seurat-v3 LOESS
reported a singular fit at the default; no silent algorithm substitution.

Every output was downloaded and checked against its size/SHA-256. All 584,944
and 1,000,000 embedding rows were checked for unique IDs, dimensions and finite
values; annotation/probability alignment, normalization and label argmax were
checked for every cell. All 42 objects per run were checked in the tenant
bucket, with independent raw-byte readback of three selected objects.

## Customer-path and failure checks

| Scenario | Outcome |
| --- | --- |
| scVI-only training via MCP | Passed; 512-cell fixture, 17 files |
| scANVI reference export → query mapping via MCP | Passed; 512-cell fixture, 27 files |
| Actual `submit.py` / `collect.py` entry points | Passed; upload reuse, training, all artifacts and bucket readback |
| Generic MCP operation/result tools | Consistent with scientific polling/results, including the large atlas run |
| Idempotent replay | Same operation, no duplicate training |
| Occupied two-slot internal key | Explicit non-admission; CLI bounded wait and same idempotency key; no limit change |
| Cancellation on final R6 | Passed; `71c69808-26a7-4884-b4cc-6c6d5b0ad370` |
| SIGTERM of actual training worker | Passed; replacement attempt restored full Lightning state and completed |
| Actual Kubernetes Pod eviction | Passed; controller retried automatically, full state restored, valid results published |
| Fresh 14,326,834,896-byte upload | Passed; 232.85s transfer + 113.54s whole-file checksum verification |
| Public website/API routing after artifact timeout fix | All nine checks passed |

Fault tests use small synthetic data to establish recovery behavior, **not**
million-cell scientific quality. Both required two execution attempts and
retained the same operation ID. This is application training checkpointing
(optimizer, scheduler, loop and RNG state), not CUDA/GPU process snapshotting.
Abrupt host power loss without a graceful signal was not separately injected.

## Fixes delivered

- Durable scientific-batch scVI/scANVI with raw `X`, `raw.X` or counts-layer
  selection, HVGs/provided genes/all genes, annotation probabilities, reusable
  references and query mapping. Existing small-file native API remains separate.
- No fixed cell/epoch cap in this new lane. Explicit memory preflight and
  configurable training, resource profiles, output budget and up-to-14-day jobs.
- Resumable S3 multipart through existing tenant-owned immutable artifacts;
  streamed client buffers, persisted transfer completion and verified-upload
  reuse after client failure. Keys are never sent to signed object-store URLs.
- Artifact-only 900s Gateway timeout; model/MCP and speech route settings remain
  unchanged. The original 40s timeout failed the first 14 GB finalization;
  that failure remains documented, and a fresh upload passed after correction.
- Correct full-state checkpoint serialization and reference-path handling;
  retryable scVI interruption exit 75 and complete failed-artifact acknowledgment.
- Generic MCP result availability now follows durable scientific publication.
- Helm's pre-existing migration-contract metadata was aligned to the already
  authoritative 38-migration contract. No new SQL migration or live RBAC change;
  the stale RBAC test now reflects the chart's existing `get,list` node access.

Final focused platform regressions: **131 passed**. Final complete Helm test
file: **139 passed**. Exact worker image: **17 runtime tests passed**, plus
actual GPU restart/reference components. Generated contract drift check passed.
This is not a full-repository-green claim: unrelated native-MD qualification
fixture and GROMACS MCP-description test failures were reproduced on the
unchanged base and remain outside this scoped delivery.

## Observability and boundaries

The admin reporting API correlates internal tenant/principal, operations,
attempts, pool, node/Pod/GPU identity, lifecycle phases and DCGM samples.
Phase occupancy reconciles but remains **estimated**, not billable utilization.
Missing distributed trace context and unavailable phase observations remain
explicitly unknown, not zero. This work does not qualify the browser admin UI.

No proof of training convergence, held-out annotation accuracy, batch mixing,
biological conservation, multi-GPU scaling, L40S performance, arbitrary atlas
fit, or LibreChat-agent quality is claimed. A single output object over 5 GiB
has not been qualified. Input transport was actually tested to 14.33 GB, below
the configurable 25 GiB input maximum. The catalog remains an active onboarding
entry; no broad production-qualification badge was fabricated.

For WhiteLab-specific validation, request a representative H5AD, the count
location, batch/donor and label columns, intended task, and a held-out evaluation
plan. These are **not blockers to platform PoC experimentation**. Customer
identity/key/workspace provisioning remains a separate explicit onboarding step.

## Retention and reproduction

- Source branch: `agent/fs2-scvi-whitelab-20261006` in
  `rene-tech/nebius-solutions-library`; based on the actual live lineage,
  `19bfdf2c3`, not an older unrelated main state. Do not deploy stale captured
  Helm values over the live shared backend.
- Private receipts, actual release/rollback state, complete artifacts, failed
  attempts and exported component data:
  `/home/tux/secure-handoff/fs2-scvi-whitelab-20261006/`. That directory contains
  private configuration; **do not commit or distribute it wholesale**.
- Hosted outputs remain in the existing system workspace at
  `qualification/scvi-whitelab-20261006/<operation-id>/main/` and as platform
  artifacts. No new bucket, customer key or user was created.
- Task-owned test Jobs/helpers and 64/128 GiB scratch PVCs are retired after
  export and hash/row verification. Raw public inputs can be downloaded again;
  completed results and failure evidence are retained. Live execution maps and
  the serving deployment remain in place.

Rebuild the safe summary from retained evidence:

```bash
python summarize.py \
  --evidence /home/tux/secure-handoff/fs2-scvi-whitelab-20261006 \
  --output results.json
```
