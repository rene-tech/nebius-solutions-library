#!/usr/bin/env python3
"""Normal deletion of explicitly owned resources; never force finalizers."""
import argparse
import json
import time
from control import ROOT, COHORT, NS, NODE, PREFIX, LABELS, PVC, APP, SOURCE, k, save, preflight

parser = argparse.ArgumentParser()
parser.add_argument("--interrupted", action="store_true")
parser.add_argument("--observe-only", action="store_true")
args = parser.parse_args()
assert COHORT in {"xjaw", "y0jt"}


def remaining(resource, name, namespace=None):
    prefix = ["-n", namespace] if namespace else []
    value = k("--request-timeout=10s", *prefix, "get", resource, name, "--ignore-not-found", "-o", "json")
    return json.loads(value) if value.strip() else None


if not args.observe_only:
    status = json.loads((ROOT / "verification.json").read_text())["status"]
    assert status == ("partial-evidence-validated" if args.interrupted else "pass")
    pods = json.loads(k("--request-timeout=10s", "-n", NS, "get", "pods", "-o", "json"))["items"]
    owned = [p for p in pods if all(p["metadata"].get("labels", {}).get(key) == value for key, value in LABELS.items())]
    if args.interrupted:
        assert all(p["metadata"]["name"] == PREFIX + "-normal-3" for p in owned)
        node = remaining("node", NODE)
        assert not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
    else:
        assert not owned
    assert all(p in owned for p in pods if any(v.get("persistentVolumeClaim", {}).get("claimName") == PVC for v in p["spec"].get("volumes", [])))
    pvc = remaining("pvc", PVC, NS)
    assert all(pvc["metadata"]["labels"].get(key) == value for key, value in LABELS.items())
    pv_name = pvc["spec"]["volumeName"]
    pv = remaining("pv", pv_name)
    assert pv["spec"]["claimRef"]["name"] == PVC and pv["spec"]["claimRef"]["namespace"] == NS
    cms = [remaining("configmap", n, NS) for n in [APP, SOURCE]]
    assert all(all(cm["metadata"]["labels"].get(key) == value for key, value in LABELS.items()) for cm in cms)
    save("lifecycle/cleanup-before.json", {"unix": time.time(), "pvc": pvc, "pv": pv, "pods": owned, "configmaps": cms})
    receipts = []
    for p in owned:
        receipts.append(k("--request-timeout=10s", "-n", NS, "delete", "pod", p["metadata"]["name"], "--wait=false"))
    receipts.append(k("--request-timeout=10s", "-n", NS, "delete", "pvc", PVC, "--wait=false"))
    receipts.append(k("--request-timeout=10s", "-n", NS, "delete", "configmap", APP, SOURCE, "--wait=false"))
    save("lifecycle/cleanup-request.json", {"unix": time.time(), "receipts": receipts, "interrupted": args.interrupted, "force": False})
    if not args.interrupted:
        preflight("final-cleanup")

before = json.loads((ROOT / "lifecycle/cleanup-before.json").read_text())
pv_name = before["pv"]["metadata"]["name"]
observed = {"observed_unix": time.time(), "cohort": COHORT, "namespace": NS, "pod": remaining("pod", PREFIX + "-normal-3", NS), "pvc": remaining("pvc", PVC, NS), "pv": remaining("pv", pv_name), "configmaps": [remaining("configmap", n, NS) for n in [APP, SOURCE]], "gpu_release": "unverified after interrupted normal3; last completed pod release is separately verified" if args.interrupted else json.loads((ROOT / "inventory/final-cleanup-gpu.json").read_text()), "normal_deletion_only": True, "force_or_finalizer_changes": False, "provider_changes": False, "deleted_material": "Task-only checkpoint PVC and immutable source/app ConfigMaps requested for normal deletion. Raw outputs, source hashes, logs and manifests remain. Checkpoint binary has no external backup.", "residual_interpretation": "A deletion acknowledgement does not prove physical reclamation. Terminating stopped-node resources must be handled by the manager without forced deletion."}
save("lifecycle/cleanup-observed.json", observed)
print(json.dumps({"cohort": COHORT, "pod_remaining": bool(observed["pod"]), "pvc_remaining": bool(observed["pvc"]), "pv_remaining": bool(observed["pv"]), "configmaps_remaining": sum(cm is not None for cm in observed["configmaps"]), "gpu_release": observed["gpu_release"]}, indent=2))
