"""Retain release/capacity evidence without request payloads or credentials."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlencode

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"


def kube(namespace, resource, name=None):
    command = ["kubectl", "--context", CONTEXT, "-n", namespace, "get", resource]
    if name:
        command.append(name)
    return json.loads(subprocess.check_output([*command, "-o", "json"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    model = kube("fs2-models", "modeldeployment", "diffdock")
    pods = kube("fs2-models", "pods")["items"]
    deployments = kube("fs2-models", "deployments")["items"]
    lynx = kube("fs2-models", "pod", "fs2-workflow-mas1-20e-a1-b307e26e3824-x8s98")
    releases = [kube("fs2-system", "deployment", name) for name in
                ("fs2-serve-control-plane", "fs2-serve-control-plane-model-controller")]
    summary = {
        "diffdock_status": model["status"],
        "model_replicas": {d["metadata"]["name"]: d["spec"].get("replicas", 0) for d in deployments},
        "diffdock_pods": [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
                           "node": p["spec"].get("nodeName"), "phase": p["status"]["phase"],
                           "conditions": p["status"].get("conditions", [])}
                          for p in pods if p["metadata"]["name"].startswith("diffdock-")],
        "lynx": {"uid": lynx["metadata"]["uid"], "phase": lynx["status"]["phase"],
                 "restarts": [c["restartCount"] for c in lynx["status"]["containerStatuses"]]},
        "releases": [{"name": d["metadata"]["name"], "images": [c["image"] for c in d["spec"]["template"]["spec"]["containers"]],
                      "ready": d["status"].get("readyReplicas", 0), "updated": d["status"].get("updatedReplicas", 0),
                      "desired": d["spec"]["replicas"]} for d in releases],
    }
    module_spec = importlib.util.spec_from_file_location("lifecycle", "/home/tux/nebius-solutions-library-inference/k8s-inference/operations/tenant-lifecycle/lifecycle.py")
    lifecycle = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(lifecycle)
    secret = kube("fs2-system", "secret", "fs2-serve-admin")
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, suffix=".token") as token:
        token.write(base64.b64decode(secret["data"]["token"]).decode())
        token.flush()
        client = lifecycle.AdminClient("https://89.169.99.188", token.name)
        try:
            query = urlencode({"runtime_image": model["spec"]["runtime"]["image"], "accelerators_per_replica": 1})
            summary["cost_policy"] = client.request("GET", "/admin/api/v1/performance/placement/diffdock?" + query)
        finally:
            client.close()
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"releases": summary["releases"], "cost_state": summary["cost_policy"]["state"], "lynx": summary["lynx"]}))


if __name__ == "__main__":
    main()
