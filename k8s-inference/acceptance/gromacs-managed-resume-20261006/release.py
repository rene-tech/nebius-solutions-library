"""Guarded image-only release; preserve unrelated services and running jobs."""

import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
REPOSITORY = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
BASELINE = REPOSITORY + "@sha256:1c22336993e588408c069b5a8f93e550ea60829f167099f55acdc1f3918fd0d9"
TARGETS = [
    ("deployment", "fs2-serve-control-plane"),
    ("deployment", "fs2-serve-control-plane-model-controller"),
    ("cronjob", "fs2-serve-control-plane-maintenance"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[0-9a-f]{64}", args.image):
        raise ValueError("Expected a digest-bound image in the existing control-plane repository")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--context", CONTEXT, "-n", "fs2-system", "--request-timeout=30s"]
    changes = []
    for kind, name in TARGETS:
        obj = json.loads(subprocess.check_output([*kube, "get", kind, name, "-o", "json"]))  # noqa: S603
        spec = obj["spec"]["jobTemplate"]["spec"] if kind == "cronjob" else obj["spec"]
        before = spec["template"]
        after = copy.deepcopy(before)
        changed = 0
        for field in ("containers", "initContainers"):
            for container in after["spec"].get(field, []):
                if container["image"].startswith(REPOSITORY + "@"):
                    if container["image"] not in (BASELINE, args.image):
                        raise ValueError("Concurrent release changed the expected baseline")
                    changed += container["image"] != args.image
                    container["image"] = args.image
        if not changed:
            continue
        after.setdefault("metadata", {}).setdefault("annotations", {})["fs2.nebius.ai/image-digest"] = args.image.split(
            "@"
        )[1]
        path = "/spec/jobTemplate/spec/template" if kind == "cronjob" else "/spec/template"
        patch = [{"op": "test", "path": path, "value": before}, {"op": "replace", "path": path, "value": after}]
        inverse = [{"op": "test", "path": path, "value": after}, {"op": "replace", "path": path, "value": before}]
        for label, data in (("patch", patch), ("rollback", inverse)):
            target = args.output / (name + "." + label + ".json")
            target.write_text(json.dumps(data, indent=2) + "\n")
        cmd = [*kube, "patch", kind, name, "--type=json", "--patch-file", str(args.output / (name + ".patch.json"))]
        subprocess.run([*cmd, "--dry-run=server", "-o", "name"], check=True)  # noqa: S603 - fixed target and digest-checked image
        changes.append(cmd)
    if args.apply:
        for command in changes:
            subprocess.run([*command, "-o", "name"], check=True)  # noqa: S603 - exact server-validated command
    print(json.dumps({"targets": len(changes), "applied": args.apply, "image": args.image}))


if __name__ == "__main__":
    main()
