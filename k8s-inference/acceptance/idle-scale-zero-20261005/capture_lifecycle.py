"""Read-only floor, owner, drain and protected-customer receipt (no Pod contents)."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from remove_hot_floors import active_counts, kubectl

PROTECTED_POD = "fs2-workflow-mas1-20e-a1-7d7046f29109-74jtt"
PROTECTED_UID = "28cba283-f42d-4fe7-aac7-010ba2222f1e"


def main(args):
    resources = json.loads(kubectl(args.context, "-n", "fs2-models", "get",
                                  "modeldeployments,deployments,scaledobjects,pods", "-o", "json"))["items"]
    pods = [r for r in resources if r["kind"] == "Pod"]
    rows = []
    for model in (r for r in resources if r["kind"] == "ModelDeployment"):
        metadata, spec, status = model["metadata"], model["spec"], model.get("status", {})
        owned = [r for r in resources if any(o["uid"] == metadata["uid"]
                 for o in r["metadata"].get("ownerReferences", []))]
        deployments = [r for r in owned if r["kind"] == "Deployment"]
        scalers = [r for r in owned if r["kind"] == "ScaledObject"]
        assert len(deployments) == 1, metadata["name"]
        disabled = spec["lifecycle"]["desiredState"] == "Disabled"
        assert len(scalers) == (0 if disabled else 1), metadata["name"]
        deployment, scaler = deployments[0], (scalers[0] if scalers else None)
        selector = deployment["spec"]["selector"]["matchLabels"]
        current_pods = [p for p in pods if all(p["metadata"].get("labels", {}).get(k) == v
                       for k, v in selector.items())]
        assert spec["availability"]["minReplicas"] == 0
        if scaler:
            assert scaler["spec"]["minReplicaCount"] == 0
            assert scaler["spec"]["scaleTargetRef"]["name"] == deployment["metadata"]["name"]
        else:
            assert deployment["spec"].get("replicas") == 0 and not current_pods
        row = {"name": metadata["name"], "uid": metadata["uid"], "generation": metadata["generation"],
               "availability": spec["availability"], "exposure": spec["exposure"], "lifecycle": spec["lifecycle"],
               "phase": status.get("phase"), "observed_generation": status.get("observedGeneration"),
               "deployment": deployment["metadata"]["name"], "deployment_uid": deployment["metadata"]["uid"],
               "replicas": deployment["spec"].get("replicas"), "scaler": scaler["metadata"]["name"] if scaler else None,
               "pods": [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
                         "node": p["spec"].get("nodeName"), "phase": p["status"].get("phase"),
                         "containers": [{"name": c["name"], "image_id": c.get("imageID"),
                                         "ready": c.get("ready"), "restarts": c.get("restartCount")}
                                        for c in p["status"].get("containerStatuses", [])]}
                        for p in current_pods]}
        if args.require_cold:
            assert row["phase"] == "Cold" and row["replicas"] == 0 and not row["pods"], row
        rows.append(row)
    protected = next((p for p in pods if p["metadata"]["name"] == PROTECTED_POD), None)
    customer = None
    if protected:
        assert protected["metadata"]["uid"] == PROTECTED_UID
        customer = {"name": PROTECTED_POD, "uid": PROTECTED_UID,
                    "created_at": protected["metadata"]["creationTimestamp"],
                    "node": protected["spec"].get("nodeName"), "phase": protected["status"].get("phase"),
                    "container_restarts": [c.get("restartCount") for c in protected["status"].get("containerStatuses", [])]}
    deployments = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "deploy",
                                   "fs2-serve-control-plane", "fs2-serve-control-plane-model-controller",
                                   "fs2-mindeval-workshop", "-o", "json"))["items"]
    receipt = {"observed_at": datetime.now(UTC).isoformat(), "context": args.context,
               "all_floors_zero": True, "require_cold": args.require_cold,
               "models": sorted(rows, key=lambda row: row["name"]), "protected_customer_pod": customer,
               "active_operations_by_model": active_counts(args.context),
               "platform": [{"name": d["metadata"]["name"], "generation": d["metadata"]["generation"],
                             "images": {c["name"]: c["image"] for c in d["spec"]["template"]["spec"]["containers"]},
                             "ready_replicas": d["status"].get("readyReplicas"),
                             "configmaps": {v["name"]: v["configMap"]["name"]
                                            for v in d["spec"]["template"]["spec"].get("volumes", []) if "configMap" in v}}
                            for d in deployments]}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"models": len(rows), "all_floors_zero": True,
                      "all_cold": all(r["phase"] == "Cold" and not r["pods"] for r in rows),
                      "protected_customer_pod_preserved": customer is not None,
                      "observed_at": receipt["observed_at"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-cold", action="store_true")
    main(parser.parse_args())
