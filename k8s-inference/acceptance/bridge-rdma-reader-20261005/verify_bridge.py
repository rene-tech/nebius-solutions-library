"""Read-only exact-reader barrier; does not claim MD workflow qualification."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from activate_bridge import (
    BASE, CATALOG_SHA, CONTEXT, IMAGE, NEW_CM, NS, PROFILE_SHA, TARGETS, template,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lynx-longrun-20261005"))
from activate_maintenance import verify as verify_maintenance  # noqa: E402

LYNX_POD = "fs2-workflow-mas1-20e-a1-7d7046f29109-74jtt"
LYNX_UID = "28cba283-f42d-4fe7-aac7-010ba2222f1e"


def readers(deployment, sets, pods, count):
    if deployment["spec"]["replicas"] != count:
        raise ValueError("Replica count changed; never override HPA to satisfy this check")
    owners = {row["metadata"]["uid"] for row in sets if any(
        owner.get("uid") == deployment["metadata"]["uid"] and owner.get("controller")
        for owner in row["metadata"].get("ownerReferences", []))}
    selected = [row for row in pods if any(
        owner.get("uid") in owners and owner.get("controller")
        for owner in row["metadata"].get("ownerReferences", []))]
    if len(selected) != count:
        raise ValueError("Missing readers or extra old/terminating readers")
    for pod in selected:
        if (pod["metadata"].get("deletionTimestamp")
                or not any(row["type"] == "Ready" and row["status"] == "True"
                           for row in pod.get("status", {}).get("conditions", []))
                or len(pod["spec"]["containers"]) != 1
                or pod["spec"]["containers"][0]["image"] != IMAGE
                or any(row.get("restartCount", 0) for row in pod["status"].get("containerStatuses", []))):
            raise ValueError("An exact-image reader is unhealthy or not converged")
        for row in pod["spec"].get("initContainers", []):
            if row["name"] == "wait-schema" and row["image"] != IMAGE:
                raise ValueError("Old strict init reader remains")
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s"]

    def run(*parts):
        return json.loads(subprocess.check_output([*kube, *parts], timeout=40))

    def get(kind, name=None, namespace=NS):
        return run("-n", namespace, "get", kind, *([name] if name else []), "-o", "json")

    def save(name, body):
        (args.output / name).write_text(json.dumps(body, indent=2) + "\n")

    sets, pods = get("replicasets")["items"], get("pods")["items"]
    identities = []
    for kind, name in TARGETS[:2]:
        live = get(kind, name)
        applied = json.loads((args.activation / (name + ".applied.json")).read_text())
        if live["metadata"]["uid"] != applied["metadata"]["uid"] or live["spec"] != applied["spec"]:
            raise ValueError("A reader spec changed after the exact reviewed activation")
        selected = readers(live, sets, pods, 3 if name == BASE else 2)
        for pod in selected:
            row = {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                   "image": pod["spec"]["containers"][0]["image"],
                   "image_id": pod["status"]["containerStatuses"][0]["imageID"]}
            if name == BASE:
                proof = run("-n", NS, "exec", row["name"], "-c", "control-plane", "--",
                            "python", "-m", "fs2_serve_catalog.cli", "validate",
                            "--catalog-root", "/opt/fs2/catalog")
                if proof.get("catalog_digest") != CATALOG_SHA or proof.get("status") != "PASS":
                    raise ValueError("Packaged catalog identity differs from bridge qualification")
                row["catalog"] = proof
                profile = run("-n", NS, "exec", row["name"], "-c", "control-plane", "--",
                              "python", "-c", "import hashlib,json,pathlib; "
                              "b=pathlib.Path('/opt/fs2/catalog/contracts/scientific-workload-profiles.json').read_bytes(); "
                              "p=next(p for p in json.loads(b)['profiles'] if p['model_id']=='gromacs-mpi'); "
                              "print(json.dumps({'sha256':hashlib.sha256(b).hexdigest(),"
                              "'shapes':sum(len(s.get('execution_shapes',[])) for s in p['workload']['stages'])}))")
                if profile != {"sha256": PROFILE_SHA, "shapes": 8}:
                    raise ValueError("Packaged MPI profile body differs from the eight-shape bridge")
                row["profiles"] = profile
            identities.append(row)
        save(name + ".json", live)
    maintenance = get("cronjob", BASE + "-maintenance")
    completed = verify_maintenance(maintenance, get("jobs")["items"], IMAGE)
    before_workshop = json.loads((args.activation / "workshop.before.json").read_text())
    workshop = get("deployment", "fs2-mindeval-workshop")
    if (workshop["metadata"]["uid"] != before_workshop["metadata"]["uid"]
            or workshop["spec"] != before_workshop["spec"]
            or workshop["status"].get("readyReplicas") != 2):
        raise ValueError("Workshop sibling differs from preserved baseline")
    before_queue = json.loads((args.activation / "before.queue.json").read_text())
    queue = get("clusterqueue", "inference-accelerators")
    if queue["metadata"]["uid"] != before_queue["metadata"]["uid"] or queue["spec"] != before_queue["spec"]:
        raise ValueError("Live queue changed; metadata-only bridge must not mutate it")
    lynx = get("pod", LYNX_POD, namespace="fs2-models")
    if (lynx["metadata"]["uid"] != LYNX_UID
            or not any(row["type"] == "Ready" and row["status"] == "True"
                       for row in lynx.get("status", {}).get("conditions", []))
            or any(row["restartCount"] != 0 for row in lynx["status"]["containerStatuses"])):
        raise ValueError("Original customer Pod is not preserved and healthy")
    api = get("deployment", BASE)
    refs = {row["name"]: row["configMap"]["name"]
            for row in template(api)["spec"].get("volumes", []) if "configMap" in row}
    if refs["scientific-batch-scheduling"] != NEW_CM:
        raise ValueError("Corrected scheduling object is not mounted")
    receipt = {"at": datetime.now(timezone.utc).isoformat(), "status": "passed", "read_only": True,
               "image": IMAGE, "readers": identities, "maintenance": completed,
               "configmaps": refs, "profile_sha256": PROFILE_SHA,
               "workshop_preserved": True, "queue_preserved": True,
               "lynx": {"pod": LYNX_POD, "uid": LYNX_UID, "ready": True, "restarts": 0},
               "schema_changed": False, "rdma_profile_published": False,
               "scientific_workflow_qualified": False}
    config = json.loads(subprocess.check_output(["crane", "config", IMAGE], timeout=40))
    labels = config["config"]["Labels"]
    if (labels["org.opencontainers.image.revision"] != "dbcb4b994caf3c2b1847a6b94d078a5bf98d37d5"
            or labels["ai.nebius.fs2-serve.source-tree"] != "76883417785f8ab59b36cd85798eec2d071f470f"):
        raise ValueError("Published image provenance differs from the committed bridge")
    receipt["registry_labels"] = labels
    save("verification.json", receipt)
    print(json.dumps({"status": "passed", "api_readers": 3, "controller_readers": 2,
                      "maintenance": "passed", "scheduling": NEW_CM,
                      "lynx_and_workshop_preserved": True}))


if __name__ == "__main__":
    main()
