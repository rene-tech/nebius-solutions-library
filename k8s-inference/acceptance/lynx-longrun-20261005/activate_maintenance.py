"""Update the existing scientific payload cleanup reader to the final API build.

Dry-run by default. Preserve its schedule, database identity, resources, and
retention configuration. Verification of a new successful Job is separate.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path

from activate_compatible_readers import CONTEXT, NAME, REPO

PREVIOUS = REPO + "@sha256:fb6d32098a2e32853aaac1eb5a6ee117b3212e06ad357aa790668f5450a18179"


def prepare(before: dict, image: str) -> list[dict]:
    if not re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError("An immutable image from the existing regional repository is required")
    template = before["spec"]["jobTemplate"]["spec"]["template"]
    desired = copy.deepcopy(template)
    containers = desired["spec"]["containers"]
    if (len(containers) != 1 or containers[0]["name"] != "maintenance"
            or containers[0]["image"] != PREVIOUS or containers[0].get("args") != ["maintenance"]):
        raise ValueError("Unexpected existing maintenance worker contract")
    containers[0]["image"] = image
    path = "/spec/jobTemplate/spec/template"
    return [
        {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
        {"op": "test", "path": path, "value": template},
        {"op": "replace", "path": path, "value": desired},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
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

    before = run("get", "cronjob", NAME + "-maintenance", "-o", "json")
    patch = prepare(before, args.image)
    save("before.cronjob.json", before)
    save("patch.json", patch)
    command = ("patch", "cronjob", NAME + "-maintenance", "--type=json", "--patch-file", str(args.output / "patch.json"))
    preview = run(*command, "--dry-run=server", "-o", "json")
    if preview["spec"]["jobTemplate"]["spec"]["template"] != patch[-1]["value"]:
        raise ValueError("Server changed the intended maintenance-only template")
    save("dry-run.cronjob.json", preview)
    if args.apply:
        save("applied.cronjob.json", run(*command, "-o", "json"))
    summary = {"applied": args.apply, "image": args.image, "new_job_verified": False}
    save("summary.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
