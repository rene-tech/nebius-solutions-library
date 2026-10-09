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


def prepare(before: dict, image: str, *, expected_image: str = PREVIOUS) -> list[dict]:
    if any(not re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", value)
           for value in (image, expected_image)):
        raise ValueError("An immutable image from the existing regional repository is required")
    template = before["spec"]["jobTemplate"]["spec"]["template"]
    desired = copy.deepcopy(template)
    containers = desired["spec"]["containers"]
    if (len(containers) != 1 or containers[0]["name"] != "maintenance"
            or containers[0]["image"] != expected_image or containers[0].get("args") != ["maintenance"]):
        raise ValueError("Unexpected existing maintenance worker contract")
    containers[0]["image"] = image
    path = "/spec/jobTemplate/spec/template"
    return [
        {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
        {"op": "test", "path": path, "value": template},
        {"op": "replace", "path": path, "value": desired},
    ]


def verify(cronjob: dict, jobs: list[dict], image: str) -> dict:
    containers = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    if len(containers) != 1 or containers[0]["image"] != image:
        raise ValueError("The maintenance CronJob is not on the final image")
    owned = [job for job in jobs if any(
        owner.get("uid") == cronjob["metadata"]["uid"] and owner.get("controller") is True
        for owner in job["metadata"].get("ownerReferences", [])
    )]
    if any(job.get("status", {}).get("active", 0) and any(
        item["image"] != image for item in job["spec"]["template"]["spec"]["containers"]
    ) for job in owned):
        raise ValueError("A previous maintenance reader is still active")
    succeeded = [job for job in owned if job.get("status", {}).get("succeeded", 0) == 1
                 and all(item["image"] == image for item in job["spec"]["template"]["spec"]["containers"])]
    if not succeeded:
        raise ValueError("No new successful maintenance Job is observed yet")
    return {"status": "passed", "image": image, "jobs": [
        {"name": job["metadata"]["name"], "uid": job["metadata"]["uid"],
         "completed_at": job["status"].get("completionTime")}
        for job in succeeded
    ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-image", default=PREVIOUS, help="Exact currently observed reader image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true", help="Read-only observation of the new scheduled worker")
    args = parser.parse_args()
    if args.apply and args.verify:
        parser.error("--apply and --verify are separate phases")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=30s", "-n", "fs2-system"]

    def run(*arguments: str) -> dict:
        return json.loads(subprocess.check_output([*kube, *arguments]))

    def save(name: str, value: object) -> None:
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    before = run("get", "cronjob", NAME + "-maintenance", "-o", "json")
    if args.verify:
        jobs = run("get", "jobs", "-o", "json")["items"]
        result = verify(before, jobs, args.image)
        save("verification.json", result)
        print(json.dumps(result))
        return
    patch = prepare(before, args.image, expected_image=args.expected_image)
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
