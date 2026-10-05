"""Bounded idle static-to-managed cutover after exact runtime registration.

Only explicitly reviewed Apps are accepted. Existing idle Deployments and
Services are backed up and deleted with UID/resourceVersion preconditions; their
same-name replacements are created by the ModelDeployment controller, never by
this script. PVCs, caches, node pools and customer operations are untouched.
"""

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path

import httpx

from prepare_managed import MODELS, TOOLS, workload_name
from remove_hot_floors import active_counts, kubectl


def direct_classifier_idle(context, resource_name):
    """The legacy endpoint did not create operations: check actual vLLM demand."""
    metrics = kubectl(context, "-n", "fs2-models", "exec", "deployment/" + resource_name,
                      "-c", "vllm", "--", "python3", "-c",
                      "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=10).read().decode())")
    values = {}
    for name in ("num_requests_running", "num_requests_waiting"):
        matches = re.findall(r"^vllm:" + name + r"(?:\{[^\n]*\})?\s+([0-9.eE+-]+)$", metrics, re.M)
        if not matches:
            raise RuntimeError("missing live vLLM demand metric " + name)
        values[name] = sum(float(value) for value in matches)
    if any(values.values()):
        raise RuntimeError("legacy classifier has in-flight demand; postpone adoption")
    return values


def main(args):
    proposals = json.loads((args.directory / "proposals.json").read_text())
    if {p["name"] for p in proposals} != set(args.models):
        raise ValueError("exact reviewed model proposal set required")
    secret = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    receipt = []
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=120, trust_env=False) as client:
        client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}).raise_for_status()
        try:
            for proposal in proposals:
                name = proposal["name"]
                if active_counts(args.context).get(name, 0):
                    raise RuntimeError("active work appeared; postpone " + name)
                if client.get("/admin/api/v1/model-deployments/" + name).status_code != 404:
                    raise RuntimeError("desired revision already exists; inspect before retry")
                kinds = "deploy,svc,sa" if name in {"sam2-1-hiera-large", "ace-step-1-5"} or name.startswith("wan2-") else "deploy,svc"
                resource_name = workload_name(name)
                demand = direct_classifier_idle(args.context, resource_name) if name.startswith("mindguard-") else None
                before = json.loads(kubectl(args.context, "-n", "fs2-models", "get", kinds, resource_name, "-o", "json"))
                if any(r["metadata"].get("ownerReferences") for r in before["items"]):
                    raise RuntimeError("refuse deletion of already owned resources")
                row = {"model": name, "before": before, "deletes": [], "direct_demand_before": demand}
                receipt.append(row)
                preview_response = client.post("/admin/api/v1/model-deployments:plan-preview", json=proposal)
                preview_response.raise_for_status()
                preview = preview_response.json()["data"]
                row["preview"] = preview
                if preview["decision"]["disposition"] != "accepted":
                    raise RuntimeError("registration preview rejected: " + str(preview["decision"]))
                response = client.post("/admin/api/v1/model-deployments:apply", json={
                    "preview_id": preview["preview_id"], "proposed_etag": preview["proposed_etag"],
                    "proposal": proposal, "idempotency_key": "idle-managed-20261005-" + name,
                })
                row["apply_status"] = response.status_code
                row["apply"] = response.json()
                response.raise_for_status()
                if active_counts(args.context).get(name, 0):
                    raise RuntimeError("new work appeared; retain static workload for " + name)
                if name.startswith("mindguard-"):
                    row["direct_demand_after"] = direct_classifier_idle(args.context, resource_name)
                # Persist recovery evidence before the only destructive step.
                (args.directory / "adoption.json").write_text(json.dumps(receipt, indent=2) + "\n")
                for previous in before["items"]:
                    kind = previous["kind"]
                    current = json.loads(kubectl(args.context, "-n", "fs2-models", "get", kind, resource_name, "-o", "json"))
                    if current["metadata"]["uid"] != previous["metadata"]["uid"] or current.get("spec") != previous.get("spec"):
                        raise RuntimeError("static resource changed after preview; do not delete")
                    path = ("/apis/apps/v1" if kind == "Deployment" else "/api/v1")
                    plural = {"Deployment": "deployments", "Service": "services", "ServiceAccount": "serviceaccounts"}[kind]
                    path += "/namespaces/fs2-models/" + plural + "/" + resource_name
                    options = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background",
                               "preconditions": {"uid": current["metadata"]["uid"],
                                                 "resourceVersion": current["metadata"]["resourceVersion"]}}
                    subprocess.run(["kubectl", "--context", args.context, "delete", "--raw", path, "-f", "-"],
                                   input=json.dumps(options), text=True, check=True, capture_output=True)
                    row["deletes"].append({"kind": kind, "name": resource_name, "uid": current["metadata"]["uid"],
                                           "preconditioned": True})
                print(json.dumps({"model": name, "desired_applied": True, "static_resources_released": True}), flush=True)
        finally:
            client.delete("/admin/api/v1/session")
            (args.directory / "adoption.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--models", choices=tuple(TOOLS), nargs="+", default=MODELS)
    main(parser.parse_args())
