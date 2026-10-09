"""Roll only the long-run compatibility reader, preserving every live binding.

Default is a server dry-run. This intermediate image retains legacy admissions;
activate the final release only after all incompatible readers have exited.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
from pathlib import Path

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NAME = "fs2-serve-control-plane"
REPO = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/" + NAME
PREVIOUS = REPO + "@sha256:f02e712a5dfb834a054d653ec25062c9946f94e32560abc404e9f989616ce5e5"
COMPATIBLE = REPO + "@sha256:c42522ef54192f4f00081f4f157354c017553aa9a95b69496ca11c476b5dbf22"


def prepare(before: dict) -> list[dict]:
    template = before["spec"]["template"]
    desired = copy.deepcopy(template)
    containers = desired["spec"]["containers"]
    if len(containers) != 1 or containers[0]["name"] != "control-plane":
        raise ValueError("Unexpected API container topology")
    if containers[0]["image"] != PREVIOUS or before["spec"]["replicas"] != 3:
        raise ValueError("Unexpected initial API image or replica count")
    if before["spec"].get("strategy", {}).get("type") != "RollingUpdate":
        raise ValueError("Reader expansion requires the existing rolling strategy")
    containers[0]["image"] = COMPATIBLE
    return [
        {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
        {"op": "test", "path": "/spec/template", "value": template},
        {"op": "replace", "path": "/spec/template", "value": desired},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", "fs2-system"]

    def run(*arguments: str) -> dict:
        return json.loads(subprocess.check_output([*kube, *arguments]))

    def save(name: str, value: object) -> None:
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    before = run("get", "deployment", NAME, "-o", "json")
    patch = prepare(before)
    save("before.deployment.json", before)
    save("patch.json", patch)
    command = ("patch", "deployment", NAME, "--type=json", "--patch-file", str(args.output / "patch.json"))
    preview = run(*command, "--dry-run=server", "-o", "json")
    if preview["spec"]["template"] != patch[-1]["value"]:
        raise ValueError("Server changed the intended compatibility-only template")
    save("dry-run.deployment.json", preview)
    if args.apply:
        applied = run(*command, "-o", "json")
        save("applied.deployment.json", applied)
        if applied["spec"]["template"] != patch[-1]["value"]:
            raise ValueError("Applied template differs; inspect before continuing")
    summary = {"applied": args.apply, "image": COMPATIBLE, "rollout_verified": False,
               "scope": "API image only; tools, maps, routes and workers unchanged"}
    save("summary.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
