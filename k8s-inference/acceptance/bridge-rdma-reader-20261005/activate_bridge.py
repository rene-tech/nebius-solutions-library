"""Guarded schema-38 bridge: compatible readers first, unchanged eight MPI shapes.

Only three reviewed workload templates and one immutable scheduling ConfigMap
may change. No migration, capacity, quota, queue, publication, or customer write.
The exact source build was qualified separately; this helper does not build HEAD.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gang-pool-cardinality-20261005"))
from prepare_capacity import reconcile  # noqa: E402

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NS = "fs2-system"
BASE = "fs2-serve-control-plane"
REPO = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
OLD = REPO + "@sha256:191e2c2be4c131c78fd190d620f71dc2b6cfff285c21ab1af4acfb452684c195"
IMAGE = REPO + "@sha256:872b7d58cf275f2bc7e7d396e626fd1418285e65f25047ed8fbd35ab7af9f6dd"
BEFORE_SHA = "8c76afc02ad58653edcc4ecd61d966935a09a01a94e4724d4d03441181fe736c"
AFTER_SHA = "0ae63eb7efec4af40deb5c5d56d0267aeb4d92b995852625b7d55afd19c3094f"
CATALOG_SHA = "3e62475ce484705dfb28f0f568b5d6311956c645cc51a4f10030cbb9716c9ecc"
PROFILE_SHA = "5799f04002c25451bf922058d6b35ae07fbf41a830a87c7c3ee788e802d354dd"
OLD_CM = "fs2-scientific-scheduling-" + BEFORE_SHA[:12]
NEW_CM = "fs2-scientific-scheduling-" + AFTER_SHA[:12]
TARGETS = [("deployment", BASE), ("deployment", BASE + "-model-controller"),
           ("cronjob", BASE + "-maintenance")]
PREFIX = "fs2.nebius.ai/"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def template(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]


def prepare(obj):
    """Return fresh guarded forward/inverse patches; retain every other field."""
    name = obj["metadata"]["name"]
    if (obj["metadata"]["namespace"] != NS
            or (obj["kind"].lower(), name) not in TARGETS):
        raise ValueError("Unexpected bridge target")
    before = template(obj)
    after = copy.deepcopy(before)
    spec = after["spec"]
    role = ("control-plane" if name == BASE else
            "model-controller" if name.endswith("-model-controller") else "maintenance")
    if (len(spec["containers"]) != 1 or spec["containers"][0]["name"] != role
            or spec["containers"][0]["image"] != OLD):
        raise ValueError("Reader image or container inventory changed")
    spec["containers"][0]["image"] = IMAGE
    annotations = after.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[PREFIX + "image-digest"] = IMAGE.split("@", 1)[1]
    if name == BASE:
        inits = [row for row in spec.get("initContainers", []) if row["name"] == "wait-schema"]
        if len(inits) != 1 or inits[0]["image"] != OLD:
            raise ValueError("Strict wait-schema reader differs from reviewed image")
        inits[0]["image"] = IMAGE
        values = {"FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE": (OLD, IMAGE),
                  "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256": (BEFORE_SHA, AFTER_SHA)}
        for key, (old, new) in values.items():
            matches = [row for row in spec["containers"][0].get("env", []) if row["name"] == key]
            if len(matches) != 1 or matches[0].get("value") != old:
                raise ValueError("Expected exact existing " + key)
            matches[0]["value"] = new
        volumes = [v for v in spec.get("volumes", []) if v["name"] == "scientific-batch-scheduling"]
        if len(volumes) != 1 or volumes[0].get("configMap", {}).get("name") != OLD_CM:
            raise ValueError("Scheduling ConfigMap reference changed")
        volumes[0]["configMap"]["name"] = NEW_CM
        annotations.update({PREFIX + "scientific-scheduling-sha256": AFTER_SHA,
                            PREFIX + "scientific-profiles-sha256": PROFILE_SHA,
                            PREFIX + "catalog-rollout-digest": "sha256:" + CATALOG_SHA})
    path = "/spec/" + ("jobTemplate/spec/" if obj["kind"] == "CronJob" else "") + "template"
    guard = [{"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]}]
    if obj["kind"] == "CronJob":
        # Scheduled status updates change resourceVersion every minute. Guard all
        # configuration instead, still rejecting schedule, suspend or policy edits.
        guard.append({"op": "test", "path": "/spec", "value": obj["spec"]})
    else:
        guard.append({"op": "test", "path": "/metadata/resourceVersion",
                      "value": obj["metadata"]["resourceVersion"]})
    forward = [*guard, {"op": "test", "path": path, "value": before},
               {"op": "replace", "path": path, "value": after}]
    # Deliberately no stale resourceVersion in the inverse: exact desired template
    # and original UID guard rollback after normal Kubernetes status updates.
    inverse = [guard[0], {"op": "test", "path": path, "value": after},
               {"op": "replace", "path": path, "value": before}]
    return forward, inverse


def configmap(old, queue):
    if (old["metadata"]["name"] != OLD_CM or old.get("immutable") is not True
            or set(old["data"]) != {"kueue-scheduling.json"}):
        raise ValueError("Unexpected existing scheduling object")
    contract = json.loads(old["data"]["kueue-scheduling.json"])
    if digest(contract) != BEFORE_SHA:
        raise ValueError("Existing scheduling body has drifted")
    desired = reconcile(contract, queue, configured_max_nodes=8, configured_gpus_per_node=1)
    if digest(desired) != AFTER_SHA:
        raise ValueError("Correction differs from the reviewed eight-field metadata change")
    return {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"namespace": NS, "name": NEW_CM,
                         "annotations": {"fs2-serve.nebius.ai/scheduling-contract-sha256": AFTER_SHA}},
            "data": {"kueue-scheduling.json": canonical(desired)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", NS]

    def run(*parts):
        return json.loads(subprocess.check_output([*kube, *parts], timeout=40))

    def save(name, body):
        path = args.output / name
        path.write_text(json.dumps(body, indent=2) + "\n")
        return path

    old_cm = run("get", "configmap", OLD_CM, "-o", "json")
    queue = run("get", "clusterqueue", "inference-accelerators", "-o", "json")
    cm = configmap(old_cm, queue)
    save("before.configmap.json", old_cm)
    save("before.queue.json", queue)
    cm_path = save("configmap.json", cm)
    save("configmap.client-dry-run.json", run("create", "-f", str(cm_path), "--dry-run=client", "-o", "json"))
    save("configmap.server-dry-run.json", run("create", "-f", str(cm_path), "--dry-run=server", "-o", "json"))
    before = {}
    for kind, name in TARGETS:
        obj = run("get", kind, name, "-o", "json")
        before[name] = obj
        patch, inverse = prepare(obj)
        save(name + ".before.json", obj)
        path = save(name + ".patch.json", patch)
        save(name + ".inverse.json", inverse)
        preview = run("patch", kind, name, "--type=json", "--patch-file", str(path),
                      "--dry-run=server", "-o", "json")
        if template(preview) != patch[-1]["value"]:
            raise ValueError("Server changed the reviewed bridge template")
        save(name + ".server-dry-run.json", preview)
    save("workshop.before.json", run("get", "deployment", "fs2-mindeval-workshop", "-o", "json"))
    if args.apply:
        # All target dry runs pass before the first write. The additive immutable
        # object is harmless until a guarded API template references it.
        save("configmap.applied.json", run("create", "-f", str(cm_path), "-o", "json"))
        for kind, name in TARGETS:
            fresh = run("get", kind, name, "-o", "json")
            if fresh["metadata"]["uid"] != before[name]["metadata"]["uid"] or fresh["spec"] != before[name]["spec"]:
                raise ValueError("Workload spec changed between preview and activation")
            patch, inverse = prepare(fresh)
            path = save(name + ".fresh.patch.json", patch)
            save(name + ".fresh.inverse.json", inverse)
            save(name + ".applied.json", run("patch", kind, name, "--type=json", "--patch-file", str(path), "-o", "json"))
    receipt = {"applied": args.apply, "image": IMAGE, "scheduling_configmap": NEW_CM,
               "catalog_sha256": CATALOG_SHA, "profile_sha256": PROFILE_SHA,
               "schema": 38, "mpi_shapes": 8, "verified_ready": False}
    save("summary.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
