#!/usr/bin/env python3
"""Publish guarded conclusions, preserving startup losses and quality caveats."""
import argparse
import json
from pathlib import Path
import statistics
from control import ROOT, BASE, COHORT, NODE, IMAGE, TOOLS, PVC

parser = argparse.ArgumentParser()
parser.add_argument("--complete", action="store_true")
parser.add_argument("--archive-original", action="store_true", help="Regenerate fkt entrypoints without overwriting the overall lane result")
args = parser.parse_args()
assert not args.archive_original or COHORT == "fkt"
result_path = ROOT / ("fkt-result.json" if args.archive_original else "result.json")
report_path = ROOT / ("fkt-report.md" if args.archive_original else "report.md")
data = json.loads((ROOT / "analysis.json").read_text())
interrupted = (ROOT / "incident/node.json").is_file()
groups = {row["variant"]: row for row in data["summaries"]}
matched = [row for row in data["paired_outputs"] if row["matched_request_history"] and "-restore-" in row["pod"]]
normal = groups.get("normal", {})
restored = groups.get("restore", {})
if args.complete:
    assert normal["n"] == restored["n"] == 3
    assert len(matched) == 6
    assert all(row["status"] == "passed" and row["sequence_valid"] and row["graph_verified"] for row in data["attempts"])
    assert (ROOT / "inventory/matrix-complete-gpu.json").is_file()
ratio = (restored["container_to_ready_seconds"]["median"] / normal["container_to_ready_seconds"]["median"]) if normal and restored else None
gpu_bounds = [sum(row.get("gpu_slot_seconds_bounds", [0, 0])[i] for row in data["lifecycle"]) for i in [0, 1]]
identity = data["checkpoint_capture"]["runtime_identity"]
quality = {"native_validation": "finite coordinates, chain sequence/residue cardinality, confidence envelope, exact checkpoint/provenance, successful forward and actual graph state", "matched_history_output_pairs": matched, "byte_identical_to_normal_count": sum(row["byte_identical"] for row in matched), "paired_count": len(matched), "scientific_parity": "NOT QUALIFIED: experimental-reference regression in Protenix model lane persists independently of snapshot results", "metric": "Full matching-sequence per-chain CA-lDDT;15A pairs,0.5/1/2/4A thresholds; not all-atom lDDT or interface quality", "reference_scores": [{"pod": row["pod"], "case": row["case"], "label": row["label"], "scores": [score for output in row["outputs"] for score in output["reference_ca_lddt"]]} for row in data["attempts"]]}
quality["within_cohort_numerical_dispersion"] = data["within_cohort_numerical_dispersion"]
has_fallback = "fallback" in groups
result = {"models": [{"model_id": "protenix-v2", "bir_fit": "Public BIR0.1.0 model module in exact current Protenix v2 featurizer/dumper/validator prototype", "status": "measured" if args.complete else "partial", "baseline": {"variant": "ordinary load of identical persistent BIR worker on same H100", "request_history": "load→warm76→129→76", "summary": normal}, "comparisons": [{"variant": "fresh-pod CUDA/CRIU restore", "request_history": "donor load→warm76→capture→donor deletion→fresh restore→129→76", "summary": restored, "restore_over_normal_ready_ratio": ratio, "first_validated_output_ratio": None, "first_output_ratio_exclusion": "normal first input76; restored first input129. Keep raw clocks, do not divide unlike request histories."}], "quality": quality, "features": {"three_fresh_restore_cycles": restored.get("n"), "different_shape_after_restore": "129-aa graph capture followed by76-aa graph replay", "seed": 101, "cycles": 10, "diffusion_steps": 200, "samples": 1, "dtype": "bf16 serving profile with BIR mixed-precision implementation", "msa_templates": "disabled exactly as model lane", "request_routing": "pod-local Python HTTPServer, no external Service/Ingress or production label", "fallback": "intentional model revision mismatch, normal-load recovery; separately retained"}, "snapshot": {"mechanism": "NVIDIA cuda-checkpoint plus CRIU process dump/restore; not CUDA Graph capture alone", "identity": identity, "capture": data["checkpoint_capture"], "lifecycle": data["lifecycle"], "startup_summaries": data["summaries"], "gpu_slot_seconds_bounds": gpu_bounds, "gpu_slot_seconds_per_validated_prediction_bounds": [value / len(data["attempts"]) for value in gpu_bounds] if data["attempts"] else None, "gpu_cost_scope": "Includes loading, troubleshooting, capture/flush, request execution, collection, idle gaps and deletion. Slot-time proxy, not GPU compute time or production throughput."}, "recommendation": "Do not promote. Technical restore behavior is recorded; scientific parity is unqualified and this storage-backed restore has no demonstrated startup benefit.", "limitations": data["limitations"] + ["The initial donor first-output delay includes failed CPU-preparation attempts and operator time; excluded from matched startup ratios.", "The captured graph/history changes output relative to the first donor sample; causal snapshot comparisons use the matched normal history, not donor-first-output alone."], "evidence": ["analysis.json", "manifests/", "inventory/", "lifecycle/", "raw/", "control.py", "operate.py", "matrix.py", "request.py", "summarize.py"]}]}
result_path.write_text(json.dumps(result, indent=2) + "\n")
model = result["models"][0]
model["limitations"] = [item.replace("The captured graph/history changes output relative to the first donor sample; causal snapshot comparisons use the matched normal history, not donor-first-output alone.", "First donor and later restored outputs differ, but their request histories differ too. Matched-history controls and their own numerical dispersion are retained without causal attribution.") for item in model["limitations"]]
if COHORT != "fkt":
    model["evidence"] = [("../" + path if path.endswith(".py") else path) for path in model["evidence"]]
model["limitations"].append("Per-request wall_seconds ends after native wrapper and finite-coordinate/cardinality validation. Additional sequence/graph audit and controller collection occur outside that request clock; full pod allocation bounds include them.")
model["limitations"].append("Ordinary-load controls start with an empty task executable cache; restore and guarded normal-load fallback reuse the donor's captured executable cache. Fallback is a single distinct cached-start path, not another matched normal control or a repeated cache-only optimization benchmark.")
model["features"]["fallback"] = "intentional revision mismatch, normal-load recovery; see retained supervisor log" if has_fallback else "not completed in this cohort"
if has_fallback:
    model["features"]["fallback_observation"] = {"n": 1, "container_to_ready_seconds": groups["fallback"]["container_to_ready_seconds"]["median"], "fresh_loaded_worker_not_restored": True, "reuses_donor_executable_cache": True, "not_a_matched_normal_control": True, "evidence": "fallback-verification.json"}
    model["evidence"].append("fallback-verification.json")
if COHORT != "fkt":
    model["limitations"] = [item for item in model["limitations"] if not item.startswith("Initial donor includes") and not item.startswith("The initial donor first-output delay")]
    model["limitations"].append("Initial donor setup is separate from matched normal/restore controls. Failed CPU-preparation and manual orchestration delays belong to the original fkt cohort, not this replacement.")
if COHORT == "fkt":
    model["recommendation"] = "Hold: original node stopped during third restore. Two real restores are retained, but matched normal controls/fallback were not run; no startup ratio or numerical-parity conclusion."
    model["snapshot"]["interrupted_cohort"] = {"completed_restores": 2, "interrupted_restore": "bir-protenix-snapshot-restore-3", "controls": 0, "reason": "NodeNotReady22:35:29Z; provider reportsSTOPPED, initiating cause not attributed here", "evidence": "incident/", "cleanup": "normal exact-name deletion requested; reclamation remains unverified; no force deletion"}
    model["snapshot"]["gpu_cost_scope"] += " Excludes interrupted restore3 lifetime whose release is unobserved; do not treat closed-pod sum as total experiment cost."
elif interrupted:
    model["recommendation"] = "Hold: three real restores and two controls were measured; replacement node became NotReady during normal-control3 before observed readiness. No further retries authorized. Fallback unrun, numerical/scientific equivalence unqualified; observed3v2 timing ratio is descriptive, not an acceptance pass."
    model["snapshot"]["interrupted_cohort"] = {"completed_restores": 3, "completed_normal_controls": 2, "interrupted_normal_control": "bir-protenix-xjaw-normal-3", "fallback": "unrun", "reason": "NodeNotReady during ordinary loading, before observed model readiness or inference; initiating cause not attributed", "evidence": "incident/", "cleanup": "normal exact-name requests only; final GPU memory release unverified on unreachable node"}
    model["snapshot"]["gpu_cost_scope"] += " Excludes interrupted normal3 lifetime whose release is unobserved; do not treat closed-pod sum as total experiment cost."
    model["comparisons"][0]["partial_comparison"] = True
if (BASE / "cleanup.json").exists():
    closure = json.loads((BASE / "cleanup.json").read_text()).get("cohorts", {}).get(COHORT)
    if closure:
        model["resource_closure"] = closure
        if not closure["pods"] and closure["pvc"] is None and closure["pv"] is None and not any(closure["configmaps"]):
            if "interrupted_cohort" in model["snapshot"]:
                model["snapshot"]["interrupted_cohort"]["cleanup"] = "Final normal reclamation confirmed: task pods/PVC/PV/ConfigMaps absent. Earlier pending receipts are historical. No force/finalizer/provider changes."
result_path.write_text(json.dumps(result, indent=2) + "\n")
rows = []
for group in data["summaries"]:
    rows.append("| " + " | ".join([group["variant"], str(group["n"]), *(f'{group[field]["median"]:.3f}' if field in group else "pending" for field in ["container_to_ready_seconds", "create_to_ready_seconds", "first_request_seconds"])]) + " |")
paired_scores = [score for row in matched for score in row["ca_lddt_to_comparison"]]
dispersion_lines = []
for variant in ["restore", "normal"]:
    for case in ["ubiquitin-76", "lysozyme-129"]:
        scores = [score for row in data["within_cohort_numerical_dispersion"] if row["variant"] == variant and row["case"] == case for score in row["ca_lddt"]]
        if scores:
            dispersion_lines.append(f"- {variant}, {case}: within-cohort paired CA-lDDT {min(scores):.6f}–{max(scores):.6f} ({len(scores)} pair comparisons).")
report = f'''# Protenix BIR fresh-pod snapshot qualification

Status: {"measured" if args.complete else "partial / infrastructure-blocked" if interrupted else "running"}. No production promotion or scientific-parity approval.

{"Original fkt VM became NotReady22:35:29Z during restore3; provider reportedSTOPPED. Cause is not attributed. Two restores succeeded, zero normal controls/fallback completed; no matched timing ratio is available. Original interrupted pod/PVC require coordinated cleanup; closed-pod cost excludes this unresolved lifetime. See incident/." if COHORT == "fkt" else "Independent fresh xjaw donor/GPU/checkpoint cohort; original fkt receipts are retained separately in the parent directory. No UUID remapping. Replacement node also became NotReady during normal-control3 BEFORE observed readiness/inference. Three restores and two controls completed; fallback unrun. Stop boundary reached; no additional node retry. Timing ratio is a descriptive3v2 observation, not planned acceptance completion." if interrupted else "Independent fresh xjaw donor/GPU/checkpoint cohort; no UUID remapping."}

## Outcome

Three same-node fresh restores and three ordinary-load controls are required. Current completion: {restored.get("n",0)} restores, {normal.get("n",0)} controls. The matched readiness ratio (restore/ordinary load) is {f"{ratio:.3f}×" if ratio else "pending"}; values above1 mean slower restoration, not acceleration.

Matched-history restored outputs: {sum(row["byte_identical"] for row in matched)}/{len(matched)} byte-identical to normal control1; paired CA-lDDT range {f"{min(paired_scores):.6f}–{max(paired_scores):.6f}" if paired_scores else "pending"}. This is a small technical cohort, not scientific equivalence. The underlying model-lane experimental-reference quality regression remains disqualifying even if snapshot/normal outputs agree.

Numerical dispersion is retained separately; no causal attribution is made:

{chr(10).join(dispersion_lines) or "Pending repeated cohorts."}

## Exact binding and control

- Runtime: `{IMAGE}`; public `bionemo-ir==0.1.0`, PyTorch2.12.0+cu130, Python3.12.
- Tools: `{TOOLS}`. Exact frozen application/snapshot helper hashes: [source hashes](inventory/source-hashes.json).
- Source/checkpoint binding: source2475421477ab414b571149ad4a875c390ff8a35d, checkpoint8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599; manifesta093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7.
- Same node `{NODE}`, GPU `{identity["gpu_uuid"]}`, driver `{identity["driver_version"]}`, kernel `{identity["kernel_release"]}`; no remapping.
- Exact global-RNG/token-transformer-graph prototype from model lane. Serving remains Python HTTPServer, without a uvloop compatibility change. Read-only host-local public model artifacts; own128Gi RWO snapshot PVC. GPU allocated normally by Kubernetes, one device per pod.
- Each prediction retains seed101,10 cycles,200 steps,one sample,no MSA/templates/RNA, native input/provenance/confidence/cardinality validation. Structures additionally require exact amino-acid sequence, finite coordinates and actual `GRAPH_VERIFIED` capture state.
- Capture follows donor76-aa ubiquitin warmup and synchronous `/prepare-snapshot` CUDA drain. Actual CUDA owner PID{data["checkpoint_capture"]["cuda_pids"][0]} is dumped, not its supervisor. Donor deletion and free-GPU checks precede every fresh restore. New129-aa lysozyme is then evaluated before replaying76-aa input.
- Normal controls use load→76→129→76, matched against donor76→capture→restore→129→76. Readiness ends before normal76 warmup. First-output shapes differ, so their raw clocks are retained but not converted into a misleading first-output speed ratio.

## Timings

| Cohort | n | Container→ready median,s | Pod-create→ready median,s | First validated request median,s |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

Capture: {data["checkpoint_capture"]["records"][1]["seconds"]:.3f}s CUDA checkpoint, {data["checkpoint_capture"]["records"][2]["seconds"]:.3f}s CRIU dump, plus {data["checkpoint_capture"]["checkpoint_flush_seconds"]:.3f}s durable flush. Captured pages≈5.395GB; generated executable cache≈16.8MB. Image pull/init/mount, CRIU restore, request and graph phases are separate in [analysis](analysis.json), lifecycle files and supervisor logs. The original fkt donor pulled its image in6.01s; replacement xjaw reuses its already-cached image. Neither is silently treated as a cold-start advantage. Restored health `model_load_seconds` is inherited donor metadata, never a restore stopwatch.

Ordinary-load controls have an empty task executable cache. Restore and guarded normal-load fallback reuse the donor's captured executable cache; this is part of the measured deployment path. Fallback is reported separately and is not pooled into the three normal controls. Its single cached-start observation does not qualify a cache-only optimization or a matched request-history startup comparison.

Allocated GPU-slot proxy bounds across all retained pods: {gpu_bounds[0]:.3f}–{gpu_bounds[1]:.3f}s, including diagnostic/operator gaps and durable flushing. This is not saturated production throughput or GPU compute time.

Per-request clocks end after the native wrapper and finite-coordinate/cardinality validation. Extra sequence/graph audit and evidence collection occur outside that request clock; they remain mandatory success gates and are included in the allocated-pod windows. Readiness polling and controller overhead are retained, not subtracted.

## Failures and limitations

In the original fkt cohort, the first two warm-client attempts failed before inference: CPU `prep` was initially routed into the prediction-only bridge; an unbridged retry found the GPU candidate lacks `zstd` for packaging CPU handoffs. All12 failed CPU-preparation stage records and logs remain in the original cohort, not counted as replacement failures. Final comparisons reuse byte-hashed validated CPU handoffs from the exact current-model lane, as its benchmark does. No model, confidence or artifact validator was bypassed.

The donor first76 output differs from its post-restore76 result after129. That observation alone is confounded by graph/request history; it is not described as tiny or automatically attributed to checkpointing. Matched-history normal controls and their own dispersion inform comparisons but do not establish a cause. CA-lDDT against experimental references remains descriptive and does not establish all-atom quality, interface quality or non-inferiority.

Observed compatibility is limited to this GPU UUID, image, source, kernel and driver. No cross-GPU remapping, version upgrades, arbitrary batch envelope, concurrency/cancellation, production route or customer workload was exercised here. Model-lane broader native feature evidence remains separate.

## Reproduction and disposition

Review capacity and ownership first. `control.py prepare`, `control.py donor`, `operate.py ready/request/capture/release` freeze and capture the task-owned worker. `matrix.py` performs three restores, three controls and a revision-mismatch fallback; `summarize.py` and `write_report.py --complete` regenerate results from raw records. Select `BIOIR_SNAPSHOT_COHORT=xjaw` for replacement artifacts; `run_fresh.py` orchestrates that complete fresh cohort. Original manual restore1/2 and local-controller interruptions are retained separately.

No host configuration, driver, production model or routing changes. GPU/Task Deck skills guided node preflight, scoped resource ownership and retained phase/quality evidence; the Kubernetes diagnostic workflow stopped the interrupted cohorts at node unavailability without unsafe host intervention. Cleanup disposition is recorded separately in [lifecycle](lifecycle/); no checkpoint binary needs production promotion.
'''
report = report.replace("Allocated GPU-slot proxy bounds across all retained pods", "Allocated GPU-slot proxy bounds across closed, release-verified pods only")
if interrupted:
    report += "\nThe interrupted pod lifetime and final GPU release are unverified and excluded from closed-pod cost bounds. Normal cleanup acknowledgements do not prove disk reclamation.\n"
if has_fallback:
    report += "\nFallback audit: [new-worker/clock verification](fallback-verification.json) confirms identity rejection before any restore command, a new model-loaded PID from live HTTP health (not a readiness file), and two validated new predictions. The worker log contains a copied donor prefix; the last readiness event belongs to the new worker. Cache reuse is a verified configuration difference, not proof that it alone explains the timing difference.\n"
if model.get("resource_closure"):
    closure = model["resource_closure"]
    if not closure["pods"] and closure["pvc"] is None and closure["pv"] is None and not any(closure["configmaps"]):
        report += "\nFinal resource closure supersedes earlier pending-cleanup descriptions: this cohort's task pods, PVC, PV and immutable ConfigMaps are absent through normal reclamation. Historical incident/cleanup receipts remain unchanged.\n"
        if COHORT in {"fkt", "xjaw"}:
            report += "The final saved manager receipt shows this node Ready=True again. Earlier provider STOPPED state is historical; GPU memory was not re-probed after interruption.\n"
if COHORT == "y0jt":
    report = report.replace("Independent fresh xjaw donor/GPU/checkpoint cohort; no UUID remapping.", "Independent fresh y0jt donor/GPU/checkpoint cohort on existing non-preemptible capacity; no UUID remapping. Both earlier interrupted cohorts remain separately retained.")
    report = report.replace("replacement xjaw reuses its already-cached image", "each cohort's cached/pulled image event is retained separately")
    report = report.replace("Select `BIOIR_SNAPSHOT_COHORT=xjaw`", "Select `BIOIR_SNAPSHOT_COHORT=y0jt`")
    report = report.replace("Original manual restore1/2 and local-controller interruptions are retained separately.", "Original fkt/xjaw infrastructure and local-controller interruptions are retained separately.")
report_path.write_text(report)
