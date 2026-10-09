"""Capture exact ready reader identities and authenticated public discovery.

Read-only verification. The separate cross-namespace reader audit must establish
that other serving processes do not share the production scientific database.
This is release identity/availability evidence, not successful MD qualification.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx2

from activate_mpi import CONTEXT, NAME, NAMESPACE, named


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--qa-env", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--shape-count", type=int, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", NAMESPACE]

    def run(*args):
        return subprocess.check_output([*kube, *args], timeout=45)

    def save(name, value):
        (a.output / name).write_text(json.dumps(value, indent=2) + "\n")

    deployment = json.loads(run("get", "deployment", NAME, "-o", "json"))
    sets = json.loads(run("get", "replicasets", "-o", "json"))["items"]
    owner_uids = {s["metadata"]["uid"] for s in sets if any(
        o.get("uid") == deployment["metadata"]["uid"] and o.get("controller") is True
        for o in s["metadata"].get("ownerReferences", []))}
    pods = [pod for pod in json.loads(run("get", "pods", "-o", "json"))["items"] if any(
        o.get("uid") in owner_uids and o.get("controller") is True
        for o in pod["metadata"].get("ownerReferences", []))]
    if len(pods) != deployment["spec"]["replicas"] or not pods:
        raise ValueError("rollout has extra old/terminating readers or missing replicas")
    identities = []
    for pod in pods:
        container = named(pod["spec"]["containers"], "control-plane", "API container")
        if (container["image"] != a.image or pod["metadata"].get("deletionTimestamp")
                or not any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"]["conditions"])):
            raise ValueError("every old reader must exit and every selected new reader must be Ready")
        raw = run("exec", pod["metadata"]["name"], "-c", "control-plane", "--", "cat",
                  "/opt/fs2/catalog/contracts/scientific-workload-profiles.json")
        profiles = json.loads(raw)
        mpi = next(v for v in profiles["profiles"] if v["model_id"] == "gromacs-mpi")
        shapes = sum(len(stage.get("execution_shapes", [])) for stage in mpi["workload"]["stages"])
        if shapes != a.shape_count:
            raise ValueError("published MPI shape count differs from the rollout phase")
        identities.append({"pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                           "image": container["image"], "image_id": named(pod["status"]["containerStatuses"],
                               "control-plane", "running container")["imageID"],
                           "profiles_sha256": hashlib.sha256(raw).hexdigest(), "shapes": shapes})
    values = dict(line.split("=", 1) for line in a.qa_env.read_text().splitlines() if "=" in line)
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("only the existing system/qa identity may be used")
    with httpx2.Client(base_url="https://89.169.99.188", headers={"Authorization": "Bearer " + key},
                       timeout=60, trust_env=False) as client:
        public = client.get("/v1/scientific-models")
        public.raise_for_status()
        save("scientific-discovery.json", public.json())
    save("deployment.json", deployment)
    save("verification.json", {"at": datetime.now(timezone.utc).isoformat(), "status": "passed",
         "read_only": True, "identities": identities, "public_discovery_status": public.status_code,
         "scientific_workflow_qualified": False})
    print(json.dumps({"output": str(a.output), "ready_readers": len(identities),
                      "mpi_shapes": a.shape_count, "public_discovery_status": public.status_code}))


if __name__ == "__main__":
    main()
