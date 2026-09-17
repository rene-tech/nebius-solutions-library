#!/usr/bin/env python3
"""Capture scoped read-only closure evidence; never mutate cluster resources."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

NAMESPACES = (
    "fs2-bioir-boltz2", "fs2-bioir-openfold", "fs2-bioir-coverage",
    "fs2-bioir-protenix", "fs2-bioir-snapshot",
)
CACHE_PV = "pvc-cf75a701-6ede-4d91-935e-af6116917554"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    base = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    receipts = []

    def query(*arguments):
        command = base + list(arguments)
        started = datetime.now(timezone.utc).isoformat()
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        receipts.append({"command": command, "started_at": started,
                         "completed_at": datetime.now(timezone.utc).isoformat(),
                         "return_code": result.returncode, "stderr": result.stderr})
        result.check_returncode()
        return json.loads(result.stdout) if result.stdout.strip() else None

    remaining = []
    for namespace in NAMESPACES:
        items = query("-n", namespace, "get", "pods,pvc,configmaps", "-o", "json")["items"]
        for item in items:
            meta = item["metadata"]
            if item["kind"] == "ConfigMap" and meta["name"] == "kube-root-ca.crt":
                continue
            remaining.append({"kind": item["kind"], "namespace": namespace,
                              "name": meta["name"], "uid": meta["uid"],
                              "deletion_timestamp": meta.get("deletionTimestamp"),
                              "finalizers": meta.get("finalizers", []),
                              "phase": item.get("status", {}).get("phase"),
                              "node": item.get("spec", {}).get("nodeName"),
                              "volume": item.get("spec", {}).get("volumeName")})
    deployments = query("-n", "fs2-models", "get", "deployments", "-o", "json")["items"]
    models = [{"name": row["metadata"]["name"], "uid": row["metadata"]["uid"],
               "desired_replicas": row["spec"].get("replicas", 1),
               "ready_replicas": row.get("status", {}).get("readyReplicas", 0),
               "images": [container["image"] for container in row["spec"]["template"]["spec"]["containers"]]}
              for row in deployments]
    nodes = query("get", "nodes", "-o", "json")["items"]
    cache = query("get", "pv", CACHE_PV, "--ignore-not-found", "-o", "json")
    output = {
        "schema": "fs2.bioir-final-cluster-state/v1", "read_only": True,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "remaining_evaluation_namespace_objects": remaining,
        "model_deployments": models,
        "all_desired_model_deployments_ready": all(row["ready_replicas"] >= row["desired_replicas"] for row in models),
        "node_ready_conditions": [{"name": row["metadata"]["name"],
                                    "ready": next((condition for condition in row["status"]["conditions"]
                                                   if condition["type"] == "Ready"), None)} for row in nodes],
        "previous_cache_reclamation_exception": {
            "pv": CACHE_PV, "still_exists": cache is not None,
            "phase": cache.get("status", {}).get("phase") if cache else None,
            "historical_evidence": "../snapshot/lifecycle/storage-reclamation-exception.json",
        },
        "receipts": receipts,
    }
    Path(__file__).with_name("final-cluster-state.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"remaining_objects": remaining, "models_ready": output["all_desired_model_deployments_ready"],
                      "cache_pv_exists": cache is not None}))


if __name__ == "__main__":
    main()
