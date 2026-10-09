"""Prepare an image-only reader-first rollout; never apply from this helper.

The transitional image combines the new durable reader with the exact old
catalog. Do not publish shaped requests until every old reader has exited.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path

from activate_mpi import API_IMAGE, CONTEXT, NAME, NAMESPACE, REPO, named


def prepare(deployment, image):
    if re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", image) is None or image == API_IMAGE:
        raise ValueError("expected an immutable, new reader-first image in the existing repository")
    before = deployment["spec"]["template"]
    if named(before["spec"]["containers"], "control-plane", "API container")["image"] != API_IMAGE:
        raise ValueError("legacy API image baseline changed")
    after = copy.deepcopy(before)
    named(after["spec"]["containers"], "control-plane", "API container")["image"] = image
    after.setdefault("metadata", {}).setdefault("annotations", {})["fs2.nebius.ai/image-digest"] = image.split("@")[1]
    return {
        "patch": [
            {"op": "test", "path": "/spec/template", "value": before},
            {"op": "replace", "path": "/spec/template", "value": after},
        ],
        "inverse_before_shapes": [
            {"op": "test", "path": "/spec/template", "value": after},
            {"op": "replace", "path": "/spec/template", "value": before},
        ],
        "helm_overlay": {"image": {"repository": REPO, "digest": image.split("@")[1]}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", NAMESPACE]
    before = json.loads(subprocess.check_output([*kube, "get", "deployment", NAME, "-o", "json"]))
    bundle = prepare(before, args.image)
    for name, value in {"before.deployment": before, **bundle}.items():
        (args.output / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")
    checked = json.loads(subprocess.check_output([
        *kube, "patch", "deployment", NAME, "--type=json", "--patch-file", str(args.output / "patch.json"),
        "--dry-run=server", "-o", "json",
    ]))
    if checked["spec"]["template"] != bundle["patch"][-1]["value"]:
        raise ValueError("server dry-run altered the intended reader-only template")
    (args.output / "server-dry-run.json").write_text(json.dumps(checked, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "server_dry_run": "passed", "applied": False}))


if __name__ == "__main__":
    main()
