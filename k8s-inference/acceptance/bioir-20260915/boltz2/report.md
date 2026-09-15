# Boltz2 public BioIR evaluation — 2026-09-15

Status: **measurement complete; conditional prototype**. No production promotion.

## Decision

Public BioIR is a real fit for Boltz2, but this adapter is not ready for promotion. Separate the substantial benefit of retaining upstream weights from BioIR's incremental improvement. The candidate returns scientifically plausible structures on three public targets, but changes requested CIF chain IDs; wider scientific noninferiority and serving/snapshot gates remain mandatory. No quality-equivalence, efficacy or tail-latency claim is made.

NVIDIA documents the `boltz-2` module and pipeline, H100/L40S qualification, and diffusion CUDA graphs. Affinity has a module but no pipeline. Those are compatibility statements, not our measured speedups. [Support matrix](https://docs.nvidia.com/bionemo/inference-runtime/references/support-matrix/). Public vendor benchmark results use their own methodology and are not substituted for our current-serving measurements. [Vendor benchmarks](https://docs.nvidia.com/bionemo/inference-runtime/references/benchmark/).

## Frozen experiment

- Exact currently deployed image: `93d5fae96d87dff930206cf331f170b73f2369067f0b32169e0008f0db320a90`; source `b1ebfc46ecf57f5414e0d1a6f9027bbb122c53bc`; server SHA `5bb34260291dd4ff63b932b53df98801d4c641e86ae092deccc75d64c91e3302`.
- Identical Boltz2 confidence checkpoint: HF revision `6fdef46d763fee7fbb83ca5501ccceff43b85607`, SHA `090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1`. No checkpoint or parameter-count reduction; mixed-precision differences are recorded below.
- Public `bionemo-ir==0.1.0`, CPython3.12 wheel SHA `ebbfe2808a9a56ad9838a81f887000b89a7554d674bb7a9eac130c7cf2c4e8ae`; image `2fcd9e8fff2e1c20e676657bedc56689ded0abb2fc9863618398390ed22805ce`. Public source reference `401c6fcc4a43925bcf1342b6c0979b060130b396` is not asserted to be the wheel's build commit. Private EA bundle/wheel not used. Recipe and runtime freeze retained; exact image digest is the repeatable binary artifact, while unconstrained transitive/apt rebuilds are not bit-reproducible. [Installation documentation](https://docs.nvidia.com/bionemo/inference-runtime/install/).
- Same physical H100 UUID `GPU-1d3b90d3-7eed-1abd-59e8-95141843bde0` for all H100 comparisons; driver 580.159.04. Same L40S UUID `GPU-40db87d7-eb04-ee77-ff66-a49d2b40012f` for L40S; driver 580.173.02. Never calculate cross-GPU speedups.
- H100 requests: 12 CPU / 96 GiB; limits: 32 CPU / 192 GiB. L40S has only 64 GB host RAM, so every L40S variant explicitly uses 48 GiB / 56 GiB. One real Kubernetes GPU request/limit each; isolated task resources. No customer pods moved or scaled.
- T1031/T1038/T1096 contain 95/199/464 residues with 1618/89/59 complete supplied ordinary A3M rows. All use 3 recycles, 200 sampling steps, 1 sample. Two-sample cohort preserves 200 steps. No remote MSA or query-only substitution. Fixture and reference hashes are in [manifest](fixtures/manifest.json).
- Current API rejects seeds; its stochastic baseline is uncontrolled. Resident and BIR use 42 + request index, with every seed logged. Earlier fixed-42 BIR debugging runs are separately retained, not independent quality draws. Current upstream torch 2.9.1+cu130 versus BIR torch 2.12.0+cu130: a library/runtime-package comparison, not a single-kernel causal experiment. Upstream inference uses bf16 autocast; BIR uses its configured mixed precision/fused paths.

## Timing results

HTTP timer runs from client POST through received response bytes. Separate validated wall time also includes parsing, residue/coordinate/confidence checks and reference scoring. Full individual times, min/max, quality draws, smoke and mixed serial attempts are in [statistics.json](statistics.json); n=3 does not justify p95/p99 or confidence intervals. All clients use private in-pod loopback HTTP, not production traffic. Server queue delay under concurrency, WAN/ingress/auth overhead, and isolated current-subprocess parse/load/forward phase timing are unmeasured; resident/BIR internal phases are instrumented. The repeated cases have matched shape order; the initial H100 current mixed tail uses a different permutation, so no matched mixed-tail throughput ratio is claimed.

### H100 — median HTTP seconds, three repeats per case

| Fixture (residues) | Current | Resident upstream | Resident + optional kernels/toolchain | BIR | Current/BIR | Resident/BIR |
|---|---:|---:|---:|---:|---:|---:|
| T1031 (95) | 29.891 | 6.534 | 6.367 | 2.123 | 14.08x | 3.08x |
| T1038 (199) | 30.189 | 6.920 | 6.824 | 2.271 | 13.29x | 3.05x |
| T1096 (464) | 36.148 | 12.625 | 8.815 | 3.138 | 11.52x | 4.02x |

### L40S — median HTTP seconds, three repeats per case

| Fixture (residues) | Current | Resident upstream | Resident + optional kernels/toolchain | BIR | Current/BIR | Resident/BIR |
|---|---:|---:|---:|---:|---:|---:|
| T1031 (95) | 36.412 | 8.253 | 8.318 | 2.481 | 14.68x | 3.33x |
| T1038 (199) | 36.723 | 8.711 | 9.046 | 2.595 | 14.15x | 3.36x |
| T1096 (464) | 50.765 | 22.196 | 14.419 | 5.429 | 9.35x | 4.09x |

Current launches a fresh `boltz predict --no_kernels` subprocess per request. Resident upstream retains the exact parser/features/model `predict_step`/writer and bypasses Lightning teardown. Its first H100 model load was 19.755 s, later loads approximately 0.0001 s. Do not call those residency savings BIR gains. The optional-kernel/toolchain column is an explicitly modified derivative of current, with cuEquivariance 0.8.1 and build-essential, unchanged source/checkpoint/torch.

## Scientific and feature validation

| H100 variant | T1031 mean [range] | T1038 mean [range] | T1096 mean [range] |
|---|---|---|---|
| current-h100 | 0.7957 [0.7834, 0.8085] | 0.9342 [0.9335, 0.9346] | 0.8026 [0.7972, 0.8060] |
| persistent-h100-valid | 0.7939 [0.7868, 0.8048] | 0.9376 [0.9330, 0.9437] | 0.8124 [0.8085, 0.8146] |
| persistent-tools-h100-matrix | 0.7939 [0.7868, 0.8050] | 0.9376 [0.9330, 0.9432] | 0.8125 [0.8089, 0.8143] |
| bir-h100-matrix | 0.7993 [0.7840, 0.8074] | 0.9369 [0.9324, 0.9397] | 0.8076 [0.8042, 0.8116] |

The score is sequence-aligned CA-lDDT to supplied experimental coordinates (15 Å neighbors; 0.5/1/2/4 Å thresholds), **not all-atom OpenStructure lDDT**. Reference-matched residues are 95/95, 190/199 and 459/464. Every valid output has finite coordinates, expected residue count and finite confidence/pTM in [0,1]. Offline [quality audit](evidence/quality-audit.json) additionally verifies complete sequences, chain identities and chain-aware CA geometry. Latest audit: 218 sample CIFs, 0 sequence failures and 10 chain-ID failures. Three targets/three draws are a screening result, not a formal noninferiority test; some means improve and others decrease versus resident upstream.

The 88-residue 7sfy A/C subcomplex uses only ordinary MSAs and omits reference chain B, paired MSAs and templates because current HTTP cannot represent the latter. Its original flattened score is exploratory; the offline chain-aware score is better aligned, but neither establishes DockQ/interface-quality parity. BIR output IDs A/B instead of requested A/C are a failed gate. Current's 134-residue A/B/C repeated-sequence request fails before inference because its wrapper writes distinct MSA paths for identical sequences; failures and a direct diagnostic are preserved, not silently fixed.

Candidate postprocessing explicitly returns every computed diffusion sample and preserves current confidence ranking/formula; public pipeline default best-only behavior would not suffice. Two-sample requests are validated. Nine invalid/unsupported probes preserve HTTP 422: seed/model/templates/affinity, DNA/RNA/ligand, empty polymers and zero samples. Current exposes protein-only/mmCIF, no native batch, cancellation or idempotency interface. Mixed serial distinct-shape requests exercise workload order without pretending a batch API exists. BIR's broader library features are not advertised through this unchanged adapter. Maximum eight-sample/eight-chain limits and all malformed-MSA/error paths are not exhaustively qualified.

## Cold starts, graphs and snapshots

Initial current H100 image pull: 55.901 s; blank model artifact preparation: 350.756 s. Cached current readiness is roughly 0.4 s but means CUDA/artifacts ready, **not model loaded**. BIR H100 cached-artifact model-ready setup: 11.270 s; first 95-residue HTTP request: 9.305 s. Subsequent shape timing is separately retained. L40S BIR startup/restart and new 95/199/464 shapes also ran. Shared caches were never flushed, so these are observed cold/cached cohorts, not a universal cold-start SLO. Blank-artifact BIR was not measured; the 350.756 s and 11.270 s figures must not be divided into a cold-start speedup because their cache state/readiness semantics differ.

H100 BIR telemetry confirms `CUDAGraphPreparationState.GRAPH_VERIFIED`, captured graph objects, runtime 3 recycles / 200 steps, parser/tokenizer/features/forward/writer timing and allocator counters. CUDA counter peaks can be reset by library graph preparation; they are not external lifetime high-water telemetry. Graph capture/replay is not a GPU process snapshot. The separate [snapshot lane](../snapshot/report.md) owns fresh-pod restore qualification, two distinct post-restore shapes, repeated cycles and fallback. This lane supplied the single actual GPU-owning uvicorn worker and pinned image/cache; it does not claim restore success.

## Failures and allocated-GPU economics

At this report generation: 228 deduplicated prediction attempts, 191 valid and 37 failed. Separately, 81/81 expected-error schema probes passed. Earlier backup copies are not double-counted. Startup failures are separate: unwritable BIOIR_CACHE, missing compiler in first public image, wrong unpack helper import. Runtime debugging included 5 BIR writer-adapter failures, 13 resident frozen-record transfer failures, native homomer failures, and missing optional-kernel/toolchain errors. One direct native-homomer diagnostic and one interrupted in-flight adapter request are separate from completed HTTP JSONL records. All preserved logs/attempts count toward operational friction; failed attempts are never zero-cost successes.

| Pod | GPU-seconds allocated | Valid requests | GPU-s/valid | Valid/GPU-hour | Outside recorded requests (s) |
|---|---:|---:|---:|---:|---:|
| boltz2-persistent-kernels-h100 | 48.88 | 0 | undefined | 0 | 28.88 |
| boltz2-persistent-tools-h100 | 220.42 | 21 | 10.50 | 342.99 | 33.63 |
| boltz2-persistent-cue-h100 | 165.83 | 13 | 12.76 | 282.21 | 40.07 |
| boltz2-bir-l40s | 303.89 | 21 | 14.47 | 248.78 | 233.54 |
| boltz2-bir-h100 | 165.38 | 21 | 7.88 | 457.12 | 103.70 |
| boltz2-current-l40s | 931.72 | 21 | 44.37 | 81.14 | 83.57 |
| boltz2-persistent-kernels-l40s | 52.50 | 0 | undefined | 0 | 28.54 |
| boltz2-persistent-l40s | 303.39 | 21 | 14.45 | 249.18 | 29.88 |
| boltz2-current-h100 | 1099.49 | 16 | 68.72 | 52.39 | 553.41 |
| boltz2-persistent-tools-l40s | 317.17 | 21 | 15.10 | 238.36 | 65.78 |
| boltz2-persistent-h100 | 378.66 | 21 | 18.03 | 199.65 | 177.34 |

These are actual one-GPU schedule-to-confirmed-deletion evaluation windows, including startup, debugging waits, operator gaps, evidence collection and termination—not production saturation throughput. Outside-request seconds are occupied allocation not covered by recorded client requests, **not** a claim of hardware 0% utilization throughout. Early failed/replaced pod lifetimes are retained in [events](raw/evaluation-events.stdout); only confirmed windows appear in this table, so the table sum is not falsely labeled the complete experiment cost. Warm validated seconds per request and measured forward seconds are separately available. No monetary price assumed.

All-pod bounds additionally include earlier failed/replaced workers: the Killing event bounds the missing end below, and the next one-GPU worker's schedule bounds it above. Confirmed-delete windows conservatively include acknowledgement tail; upper bounds can include brief unallocated gaps. See UID-by-UID details in statistics.json.

- H100 all-pod allocation bound: 2167.66–2173.66 GPU-s across 92 valid predictions; 23.56–23.63 GPU-s/valid, 152.37–152.79 valid/GPU-hour.
- L40S all-pod allocation bound: 2124.67–2514.67 GPU-s across 99 valid predictions; 21.46–25.40 GPU-s/valid, 141.73–167.74 valid/GPU-hour.

## Reproduction and disposition

Read [contract](contract.json), [public package pin](public-bir.json), fixture manifest and manifests first. Initialize task-only PVC/scripts, then `python3 run_cohort.py h100 current persistent bir` (or l40s) on authorized idle nodes. It renders pinned GPU pods, waits readiness, runs full matrix/features, captures logs/pod/environment/artifacts and deletes each pod only after successful evidence copy. `persistent-tools` measures the optional-kernel derivative; `persistent-kernels` reproduces the unchanged-image missing-dependency failure. `python3 summarize.py && python3 write_report.py --complete` regenerates summaries. Review capacity again before reuse; do not reuse production pods.

GPU pods are removed after evidence capture. After snapshot consumers released it, the manager-authorized coverage worker deleted `evaluation-cache` PVC. Its PV initially remained Released with CSI `VolumeFailedDelete/DeadlineExceeded`, but the [final cluster receipt](../report/final-cluster-state.json) confirms normal reclamation is now complete. The [historical storage exception](../snapshot/lifecycle/storage-reclamation-exception.json) remains; no forced deletion or finalizer changes were attempted. Only task-owned resources were deleted. Registry images remain pinned reusable evaluation artifacts. No live serving image, route, model, driver, quota or customer workload is changed. The registry-supply-chain skill guided explicit source/target pinning and non-production image packaging.
