#!/usr/bin/env python3
"""Read-only final disposition across independently owned snapshot cohorts."""
import json
import argparse
import time
from control import BASE, NS, k

parser = argparse.ArgumentParser()
parser.add_argument("--saved-manager-receipt-only", action="store_true")
args = parser.parse_args()
if args.saved_manager_receipt_only:
    # No Kubernetes/provider calls: enrich already recorded resource absence
    # using the manager's saved final cluster receipt.
    path = BASE.parents[1] / "report/final-cluster-state.json"
    receipt = json.loads(path.read_text())
    result = json.loads((BASE / "cleanup.json").read_text())
    mapping = {"fkt": "computeinstance-e00fkt1bsa4ec657sn", "xjaw": "computeinstance-e00xjaw5jqexvvnpat", "y0jt": "computeinstance-e00y0jttwekyghrznp"}
    for cohort, row in result["cohorts"].items():
        row["latest_saved_node_condition"] = next(n for n in receipt["node_ready_conditions"] if n["name"] == mapping[cohort])
        if cohort in {"fkt", "xjaw"}:
            row["gpu_release"] = "Final memory not directly re-probed after historical interruption. Latest manager receipt has this node Ready=True again; historical STOPPED evidence is not current state. Last completed pod release was separately verified."
    result["manager_closure_evidence"] = "../../report/final-cluster-state.json"
    result["manager_closure_recorded_at"] = receipt["recorded_at"]
    (BASE / "cleanup.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Enriched cleanup from saved manager receipt only; no diagnostics run")
    raise SystemExit(0)


def get(resource, name, namespaced=True):
    scope = ["-n", NS] if namespaced else []
    value = k("--request-timeout=10s", *scope, "get", resource, name, "--ignore-not-found", "-o", "json")
    return json.loads(value) if value.strip() else None


all_pods = json.loads(k("--request-timeout=10s", "-n", NS, "get", "pods", "-o", "json"))["items"]
result = {"observed_unix": time.time(), "namespace": NS, "cohorts": {}, "normal_deletion_only": True, "force_or_finalizer_changes": False, "provider_changes": False, "material_disposition": "Task-only immutable ConfigMaps and checkpoint PVCs requested for deletion where receipts exist. Predictions, manifests, source hashes and logs retained. Checkpoint binary has no backup outside its PVC; deletion receipts alone do not prove physical reclamation."}
for cohort in ["fkt", "xjaw", "y0jt"]:
    root = BASE if cohort == "fkt" else BASE / cohort
    suffix = "" if cohort == "fkt" else "-" + cohort
    prefix = "bir-protenix-snapshot" if cohort == "fkt" else "bir-protenix-" + cohort
    before_path = root / ("incident/cleanup-before.json" if cohort == "fkt" else "lifecycle/cleanup-before.json")
    if not before_path.exists():
        continue
    before = json.loads(before_path.read_text())
    pv = before["pv"]["metadata"]["name"]
    pods = [p for p in all_pods if p["metadata"]["name"].startswith(prefix + "-") and p["metadata"].get("labels", {}).get("benchmark-model") == "protenix-bir"]
    gpu_path = root / "inventory/final-cleanup-gpu.json"
    result["cohorts"][cohort] = {"pods": pods, "pvc": get("pvc", "fs2-bioir-protenix-snapshot" + suffix), "pv": get("pv", pv, False), "configmaps": [get("configmap", "fs2-bioir-protenix-snapshot-" + kind + suffix) for kind in ["app", "source"]], "gpu_release": json.loads(gpu_path.read_text()) if gpu_path.exists() else "Unverified after interrupted pod: stopped node is not reachable. Last completed pod release was separately verified.", "cleanup_receipts": str(root.relative_to(BASE) / ("incident/cleanup-request.json" if cohort == "fkt" else "lifecycle/cleanup-request.json"))}
(BASE / "cleanup.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({cohort: {"pods_remaining": len(row["pods"]), "pvc_remaining": bool(row["pvc"]), "pv_remaining": bool(row["pv"]), "configmaps_remaining": sum(cm is not None for cm in row["configmaps"])} for cohort, row in result["cohorts"].items()}, indent=2))
