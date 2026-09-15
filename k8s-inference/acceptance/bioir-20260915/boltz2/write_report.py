"""Render the lane report from recomputed measurements, without inventing missing cohorts."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--complete", action="store_true")
args = parser.parse_args()
data = json.loads((ROOT / "statistics.json").read_text())
contract = json.loads((ROOT / "contract.json").read_text())
public = json.loads((ROOT / "public-bir.json").read_text())
tables = []
for gpu, cases in data["same_gpu_comparisons"].items():
    tables += [f"### {gpu.upper()} — median HTTP seconds, three repeats per case", "",
               "| Fixture (residues) | Current | Resident upstream | Resident + optional kernels/toolchain | BIR | Current/BIR | Resident/BIR |",
               "|---|---:|---:|---:|---:|---:|---:|"]
    for name, length in [("T1031", 95), ("T1038", 199), ("T1096", 464)]:
        item = cases[name]
        v, ratios = item["http_median_seconds"], item.get("ratio_to_bir", {})
        f = lambda x: f"{x:.3f}" if x is not None else "unmeasured"
        r = lambda x: f"{x:.2f}x" if x is not None else "—"
        tables.append(f"| {name} ({length}) | {f(v.get('current'))} | {f(v.get('persistent'))} | {f(v.get('persistent_tools'))} | {f(v.get('bir'))} | {r(ratios.get('current'))} | {r(ratios.get('persistent'))} |")
    tables.append("")

allocations = ["| Pod | GPU-seconds allocated | Valid requests | GPU-s/valid | Valid/GPU-hour | Outside recorded requests (s) |",
               "|---|---:|---:|---:|---:|---:|"]
for row in data["confirmed_pod_allocations"]:
    cost = row["allocated_gpu_seconds_per_valid_request"]
    allocations.append(f"| {row['pod']} | {row['allocated_gpu_seconds']:.2f} | {row['valid_requests']} | {cost:.2f}".replace("None", "N/A") +
                       f" | {row['valid_requests_per_allocated_gpu_hour']:.2f} | {row['outside_recorded_requests_seconds']:.2f} |" if cost is not None else
                       f"| {row['pod']} | {row['allocated_gpu_seconds']:.2f} | 0 | undefined | 0 | {row['outside_recorded_requests_seconds']:.2f} |")

quality = ["| H100 variant | T1031 mean [range] | T1038 mean [range] | T1096 mean [range] |", "|---|---|---|---|"]
audit_path = ROOT / "evidence/quality-audit.json"
audit = json.loads(audit_path.read_text()) if audit_path.exists() else []
audit_text = (f"Latest audit: {len(audit)} sample CIFs, "
              f"{sum(not item['sequences_preserved'] for item in audit)} sequence failures and "
              f"{sum(not item['chain_ids_preserved'] for item in audit)} chain-ID failures.")
bound_lines = []
for gpu, bound in data["all_pod_allocation_bounds"].items():
    upper = bound["all_attempts_gpu_seconds_upper"]
    if upper is not None:
        bound_lines.append(f"- {gpu.upper()} all-pod allocation bound: {bound['all_attempts_gpu_seconds_lower']:.2f}–{upper:.2f} GPU-s across "
                           f"{bound['valid_predictions']} valid predictions; {bound['gpu_seconds_per_valid_lower']:.2f}–{bound['gpu_seconds_per_valid_upper']:.2f} GPU-s/valid, "
                           f"{bound['valid_per_gpu_hour_lower']:.2f}–{bound['valid_per_gpu_hour_upper']:.2f} valid/GPU-hour.")
    else:
        bound_lines.append(f"- {gpu.upper()}: complete all-pod allocation upper bound awaits the final pod deletion.")
for key in ("current-h100", "persistent-h100-valid", "persistent-tools-h100-matrix", "bir-h100-matrix"):
    cases = data["cohorts"].get(key, {}).get("repeated_cases", {})
    fields = []
    for case in ("T1031", "T1038", "T1096"):
        row = cases.get(case)
        fields.append(f"{row['ca_lddt_mean']:.4f} [{row['ca_lddt_min']:.4f}, {row['ca_lddt_max']:.4f}]" if row else "unmeasured")
    quality.append("| " + key + " | " + " | ".join(fields) + " |")

model = {
    "model_id": "boltz2", "bir_fit": "supported public module and build_processor pipeline",
    "status": "measured" if args.complete else "pending",
    "adoption_status": "conditional-prototype" if args.complete else "not-yet-evaluated",
    "baseline": {**contract, "evidence": "raw/live-deployment.stdout"},
    "comparisons": data["same_gpu_comparisons"],
    "quality": {"metric": contract["quality"], "formal_noninferiority": "unverified; three targets and three draws are insufficient",
                "sample_audit": "evidence/quality-audit.json", "statistics": "statistics.json"},
    "features": {"full_supplied_msa": "measured", "templates": "not accepted by current schema",
                 "ligand_rna_dna_affinity": "HTTP422 preserved; no current product support and no BIR affinity pipeline claim",
                 "two_samples": "measured; candidate explicitly returns all computed samples, not public default best-only",
                 "chain_id_fidelity": "failed: BIR CIF writer changes A,C to A,B",
                 "native_homomer": "current fails repeated-sequence MSA-path validation; retained baseline defect",
                 "batch": "no native batch endpoint; mixed-input serial workload measured",
                 "cancellation_idempotency": "not implemented by native adapter; no claim of parity beyond absence",
                 "mcp_gateway": "not directly probed; native HTTP envelope reused, end-to-end MCP qualification unverified",
                 "http_errors": "nine invalid/unsupported cases; expected422"},
    "snapshot": {"status": "separate snapshot lane", "evidence": "../snapshot/report.md",
                 "cuda_graph": "actual captured GRAPH_VERIFIED telemetry on H100; not a CUDA process snapshot",
                 "worker": "single uvicorn process owns actual GPU model; fresh-process restarts exercised distinct shapes"},
    "recommendation": "conditional prototype: resolve chain IDs, wider scientific noninferiority and snapshot/production-serving gates before adoption",
    "limitations": data["limitations"], "public_bir": public,
    "resource_accounting": {"confirmed_pod_allocations": data["confirmed_pod_allocations"],
                            "all_pod_allocation_bounds": data["all_pod_allocation_bounds"],
                            "limitations": data["allocation_limitations"]},
    "attempts": {key: data[key] for key in ("total_prediction_attempts", "total_valid_predictions", "total_failed_predictions", "schema_probe_attempts", "schema_probe_passes", "startup_failures_separate", "non_http_diagnostics_separate")},
    "evidence": ["contract.json", "fixtures/manifest.json", "statistics.json", "evidence/", "raw/", "report.md"],
    "cleanup": "GPU pods deleted after evidence collection. After snapshot consumers released it, the manager-authorized coverage worker deleted evaluation-cache PVC. Its PV initially remained Released with CSI VolumeFailedDelete/DeadlineExceeded; final read-only cluster receipt confirms the PV is now absent through normal reclamation. Historical exception retained at ../snapshot/lifecycle/storage-reclamation-exception.json; closure ../report/final-cluster-state.json. No forced deletion, finalizer or production changes."
}
(ROOT / "result.json").write_text(json.dumps({"models": [model]}, indent=2) + "\n")

report = f"""# Boltz2 public BioIR evaluation — 2026-09-15

Status: **{'measurement complete; conditional prototype' if args.complete else 'real benchmarks running; interim report'}**. No production promotion.

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

{chr(10).join(tables)}
Current launches a fresh `boltz predict --no_kernels` subprocess per request. Resident upstream retains the exact parser/features/model `predict_step`/writer and bypasses Lightning teardown. Its first H100 model load was 19.755 s, later loads approximately 0.0001 s. Do not call those residency savings BIR gains. The optional-kernel/toolchain column is an explicitly modified derivative of current, with cuEquivariance 0.8.1 and build-essential, unchanged source/checkpoint/torch.

## Scientific and feature validation

{chr(10).join(quality)}

The score is sequence-aligned CA-lDDT to supplied experimental coordinates (15 Å neighbors; 0.5/1/2/4 Å thresholds), **not all-atom OpenStructure lDDT**. Reference-matched residues are 95/95, 190/199 and 459/464. Every valid output has finite coordinates, expected residue count and finite confidence/pTM in [0,1]. Offline [quality audit](evidence/quality-audit.json) additionally verifies complete sequences, chain identities and chain-aware CA geometry. {audit_text} Three targets/three draws are a screening result, not a formal noninferiority test; some means improve and others decrease versus resident upstream.

The 88-residue 7sfy A/C subcomplex uses only ordinary MSAs and omits reference chain B, paired MSAs and templates because current HTTP cannot represent the latter. Its original flattened score is exploratory; the offline chain-aware score is better aligned, but neither establishes DockQ/interface-quality parity. BIR output IDs A/B instead of requested A/C are a failed gate. Current's 134-residue A/B/C repeated-sequence request fails before inference because its wrapper writes distinct MSA paths for identical sequences; failures and a direct diagnostic are preserved, not silently fixed.

Candidate postprocessing explicitly returns every computed diffusion sample and preserves current confidence ranking/formula; public pipeline default best-only behavior would not suffice. Two-sample requests are validated. Nine invalid/unsupported probes preserve HTTP 422: seed/model/templates/affinity, DNA/RNA/ligand, empty polymers and zero samples. Current exposes protein-only/mmCIF, no native batch, cancellation or idempotency interface. Mixed serial distinct-shape requests exercise workload order without pretending a batch API exists. BIR's broader library features are not advertised through this unchanged adapter. Maximum eight-sample/eight-chain limits and all malformed-MSA/error paths are not exhaustively qualified.

## Cold starts, graphs and snapshots

Initial current H100 image pull: 55.901 s; blank model artifact preparation: 350.756 s. Cached current readiness is roughly 0.4 s but means CUDA/artifacts ready, **not model loaded**. BIR H100 cached-artifact model-ready setup: 11.270 s; first 95-residue HTTP request: 9.305 s. Subsequent shape timing is separately retained. L40S BIR startup/restart and new 95/199/464 shapes also ran. Shared caches were never flushed, so these are observed cold/cached cohorts, not a universal cold-start SLO. Blank-artifact BIR was not measured; the 350.756 s and 11.270 s figures must not be divided into a cold-start speedup because their cache state/readiness semantics differ.

H100 BIR telemetry confirms `CUDAGraphPreparationState.GRAPH_VERIFIED`, captured graph objects, runtime 3 recycles / 200 steps, parser/tokenizer/features/forward/writer timing and allocator counters. CUDA counter peaks can be reset by library graph preparation; they are not external lifetime high-water telemetry. Graph capture/replay is not a GPU process snapshot. The separate [snapshot lane](../snapshot/report.md) owns fresh-pod restore qualification, two distinct post-restore shapes, repeated cycles and fallback. This lane supplied the single actual GPU-owning uvicorn worker and pinned image/cache; it does not claim restore success.

## Failures and allocated-GPU economics

At this report generation: {data['total_prediction_attempts']} deduplicated prediction attempts, {data['total_valid_predictions']} valid and {data['total_failed_predictions']} failed. Separately, {data['schema_probe_passes']}/{data['schema_probe_attempts']} expected-error schema probes passed. Earlier backup copies are not double-counted. Startup failures are separate: unwritable BIOIR_CACHE, missing compiler in first public image, wrong unpack helper import. Runtime debugging included 5 BIR writer-adapter failures, 13 resident frozen-record transfer failures, native homomer failures, and missing optional-kernel/toolchain errors. One direct native-homomer diagnostic and one interrupted in-flight adapter request are separate from completed HTTP JSONL records. All preserved logs/attempts count toward operational friction; failed attempts are never zero-cost successes.

{chr(10).join(allocations)}

These are actual one-GPU schedule-to-confirmed-deletion evaluation windows, including startup, debugging waits, operator gaps, evidence collection and termination—not production saturation throughput. Outside-request seconds are occupied allocation not covered by recorded client requests, **not** a claim of hardware 0% utilization throughout. Early failed/replaced pod lifetimes are retained in [events](raw/evaluation-events.stdout); only confirmed windows appear in this table, so the table sum is not falsely labeled the complete experiment cost. Warm validated seconds per request and measured forward seconds are separately available. No monetary price assumed.

All-pod bounds additionally include earlier failed/replaced workers: the Killing event bounds the missing end below, and the next one-GPU worker's schedule bounds it above. Confirmed-delete windows conservatively include acknowledgement tail; upper bounds can include brief unallocated gaps. See UID-by-UID details in statistics.json.

{chr(10).join(bound_lines)}

## Reproduction and disposition

Read [contract](contract.json), [public package pin](public-bir.json), fixture manifest and manifests first. Initialize task-only PVC/scripts, then `python3 run_cohort.py h100 current persistent bir` (or l40s) on authorized idle nodes. It renders pinned GPU pods, waits readiness, runs full matrix/features, captures logs/pod/environment/artifacts and deletes each pod only after successful evidence copy. `persistent-tools` measures the optional-kernel derivative; `persistent-kernels` reproduces the unchanged-image missing-dependency failure. `python3 summarize.py && python3 write_report.py --complete` regenerates summaries. Review capacity again before reuse; do not reuse production pods.

GPU pods are removed after evidence capture. After snapshot consumers released it, the manager-authorized coverage worker deleted `evaluation-cache` PVC. Its PV initially remained Released with CSI `VolumeFailedDelete/DeadlineExceeded`, but the [final cluster receipt](../report/final-cluster-state.json) confirms normal reclamation is now complete. The [historical storage exception](../snapshot/lifecycle/storage-reclamation-exception.json) remains; no forced deletion or finalizer changes were attempted. Only task-owned resources were deleted. Registry images remain pinned reusable evaluation artifacts. No live serving image, route, model, driver, quota or customer workload is changed. The registry-supply-chain skill guided explicit source/target pinning and non-production image packaging.
"""
(ROOT / "report.md").write_text(report)
print(model["status"])
