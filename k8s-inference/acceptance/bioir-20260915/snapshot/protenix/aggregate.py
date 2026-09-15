#!/usr/bin/env python3
"""Overall entrypoint; never pool independent physical GPU/driver cohorts."""
import argparse
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--primary", choices=["xjaw", "y0jt"], default="y0jt")
args = parser.parse_args()
for name in ["result.json", "report.md"]:
    target = ROOT / ("fkt-" + name)
    if not target.exists():
        target.write_bytes((ROOT / name).read_bytes())
cohorts = {"fkt": json.loads((ROOT / "fkt-result.json").read_text())["models"][0]}
for cohort in ["xjaw", "y0jt"]:
    if (ROOT / cohort / "result.json").exists():
        cohorts[cohort] = json.loads((ROOT / cohort / "result.json").read_text())["models"][0]
primary = cohorts[args.primary]
verification = json.loads((ROOT / args.primary / "verification.json").read_text())
assert verification["status"] in {"pass", "partial-evidence-validated"}
model = copy.deepcopy(primary)
model["snapshot"]["primary_cohort"] = args.primary + ": independent fresh donor on its exact GPU/driver; no old bundle or UUID remapping"
model["snapshot"]["prior_interrupted_cohorts"] = {name: {"model": value, "artifact_prefix": "./" if name == "fkt" else name + "/", "provider_incident": "../../report/capacity-incident-" + name + ".json"} for name, value in cohorts.items() if name != args.primary}
model["snapshot"]["qualification_scope"] = "Technical capture/fresh-pod restore only; numerical equivalence and scientific parity NOT qualified. Partial observations do not complete acceptance."
model["evidence"] = [str((ROOT / args.primary / path).resolve().relative_to(ROOT)) + ("/" if path.endswith("/") else "") for path in primary["evidence"]]
model["evidence"] += [args.primary + "/verification.json", "fkt-result.json", "fkt-report.md", "incident/", "xjaw/result.json", "xjaw/report.md", "xjaw/verification.json", "../../report/capacity-incident-fkt.json", "../../report/capacity-incident-xjaw.json", "cleanup.json"]
model["limitations"].extend(["Physical GPU UUIDs and final-cohort driver differ; cohort timings and quality are not pooled into a matched ratio.", "Earlier stopped-node pod/PVC reclamation and final GPU memory are unverified unless cleanup receipts explicitly prove otherwise. No forced finalizers or VM mutation."])
inventory = {"fkt": {"successful_predictions": 5, "successful_restores": 2, "normal_controls": 0, "interrupted_restore_before_observed_ready": 1, "failed_cpu_preparation_stages": 12, "failed_pre_inference_harness_invocations": 2, "local_orchestration_interruptions": 2, "fallback": "unrun"}, "xjaw": {"successful_predictions": 13, "successful_restores": 3, "matched_normal_controls": 2, "interrupted_normal_before_observed_ready": 1, "fallback": "unrun", "comparison": "descriptive 3v2 only; partial acceptance"}}
if "y0jt" in cohorts:
    data = json.loads((ROOT / "y0jt/analysis.json").read_text())
    groups = {row["variant"]: row for row in data["summaries"]}
    inventory["y0jt"] = {"successful_predictions": len(data["attempts"]), "successful_restores": groups.get("restore", {}).get("n", 0), "matched_normal_controls": groups.get("normal", {}).get("n", 0), "fallback": "intentional identity guard rejects snapshot then normal-load succeeds" if "fallback" in groups else "unrun", "capacity": "existing non-preemptible H100; new GPU and driver580.173.02; independent donor"}
model["failure_inventory"] = inventory
model["verification"] = verification
if (ROOT / "cleanup.json").exists():
    model["cleanup"] = json.loads((ROOT / "cleanup.json").read_text())
(ROOT / "result.json").write_text(json.dumps({"models": [model]}, indent=2) + "\n")
ratio = model["comparisons"][0]["restore_over_normal_ready_ratio"]
quality = model["quality"]
groups = {row["variant"]: row for row in model["snapshot"]["startup_summaries"]}
ready = lambda variant: groups[variant]["container_to_ready_seconds"]["median"]
rows = ["| Original fkt | 2 | 0 | Restore3 interrupted; no matched ratio |", "| Replacement xjaw | 3 | 2 | Normal3 interrupted; fallback unrun; descriptive 3v2 only |"]
if "y0jt" in inventory:
    row = inventory["y0jt"]
    rows.append(f'| Final non-preemptible y0jt | {row["successful_restores"]} | {row["matched_normal_controls"]} | {verification["status"]}; independent fresh donor and driver |')
report = f'''# Protenix BIR snapshot evaluation

Primary cohort: **{args.primary}**, technical evidence status **{verification["status"]}**. Restore container→ready median **{ready("restore"):.3f}s**, ordinary-load median **{ready("normal"):.3f}s**; ratio **{ratio:.3f}×** (above 1 means slower). Numerical and scientific equivalence are **not established**. No promotion.

| Independent cohort | Completed fresh restores | Matched normal controls | Interpretation |
| --- | ---: | ---: | --- |
{chr(10).join(rows)}

Read the [primary cohort report]({args.primary}/report.md) for exact image/source/checkpoint, GPU/driver, phase clocks, matched request histories and quality dispersion. Its {inventory[args.primary]["successful_predictions"]} real predictions passed native validation, exact sequence, finite-coordinate and actual graph-state checks. Matched-history structure byte identity: {quality["byte_identical_to_normal_count"]}/{quality["paired_count"]} against normal control1. Both repeated normal and restored outputs are compared; variation is retained without attributing its cause. First-output shapes differ, so no first-output speed ratio is claimed.

The [original fkt report](fkt-report.md) retains two valid restores, 12 failed CPU-preparation stages and two pre-inference harness failures. The [xjaw partial report](xjaw/report.md) retains three valid restores and two matched controls: 87.741s/60.411s = 1.452× slower readiness descriptively, with the missing third control and fallback explicit. Their [fkt incident](../../report/capacity-incident-fkt.json) and [xjaw incident](../../report/capacity-incident-xjaw.json) record heartbeat loss before provider Stop operations. The initiating causes remain unresolved; neither preemption nor CUDA/CRIU faults are proven. Physical GPU UUIDs and the final driver differ: **do not pool these cohorts into a matched speedup or quality claim**.

The underlying Protenix model-lane experimental-reference quality regression remains disqualifying independently of technical snapshot compatibility. No production image, route, model, driver, quota or customer workload was changed.

Verification: [primary checks]({args.primary}/verification.json), [partial xjaw checks](xjaw/verification.json). Resource disposition: [cleanup receipts and remaining objects](cleanup.json). Only exact task-owned resources receive normal deletion requests. Stopped-node pods/PVCs or cloud volumes may remain unreclaimed; no force deletion, storage-finalizer edits or stopped-VM changes. GPU diagnostics and Kubernetes ownership/incident workflows governed these boundaries.

Rebuild reports using the matching `BIOIR_SNAPSHOT_COHORT=fkt`, `xjaw` or `y0jt`; `summarize.py`, `write_report.py` (use `--complete` only for a fully completed matrix) and `verify_evidence.py` (`--partial` for xjaw). Then use `aggregate.py --primary {args.primary}`. Preserve `fkt-result.json`/`fkt-report.md` before replacing root entrypoints. New execution requires a fresh authorized capacity check; retained manifests and names must not be replayed blindly.
'''
if "fallback" in groups:
    report += f'\nThe identity-mismatch fallback rejected restore before executing any restore command, loaded a new worker and validated both inputs. Its {ready("fallback"):.3f}s container→HTTP-ready observation reused the donor executable cache; it is a single separate cached-start path, not one of the three cold-cache normal controls or a qualified cache-only optimization. See [fallback audit]({args.primary}/fallback-verification.json).\n'
if "cleanup" in model:
    remaining = {name: {"pods": len(row["pods"]), "pvc": row["pvc"] is not None, "pv": row["pv"] is not None, "configmaps": sum(cm is not None for cm in row["configmaps"])} for name, row in model["cleanup"]["cohorts"].items()}
    if len(remaining) == 3 and all(not any(row.values()) for row in remaining.values()):
        report += '\nFinal cleanup observation: all three cohorts’ task pods, PVCs, PVs and ConfigMaps are absent through normal deletion. The final y0jt H100 reports 0 MiB and no compute processes. Historical pending-reclamation receipts remain. The final manager receipt shows fkt and xjaw Ready=True again (transitions 23:17:17 and 23:16:58 UTC); their earlier STOPPED observations are historical, not current disposition. Their GPU memory was not re-probed after interruption. Manager cross-check: [final cluster state](../../report/final-cluster-state.json). No local benchmark runner remains.\n'
(ROOT / "report.md").write_text(report)
print(json.dumps({"primary": args.primary, "status": model["status"], "ratio": ratio, "cohorts": inventory}, indent=2))
