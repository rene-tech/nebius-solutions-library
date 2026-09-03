#!/usr/bin/env python3
"""Apply the rendered Proteina-Complexa plan and collect live H100 evidence.

Only the manifests the plan already contains are applied -- this script never
invents a resource.  For every variant it records the identities and timings
that cannot be recovered from inside the container:

* ``schedule_to_semantic_complete_seconds`` -- from the Job's creation stamp to
  the container's terminated stamp, the number the batch controller cares about.
* the image phase, taken from the pod's own ``Pulled`` event, which states
  whether the image was already node-local or was pulled for this run.  The
  cache level in the receipt is that observation, not an assumption.
* node, GPU class, capacity source and container id, so a run can be traced
  back to the exact device it used.

Node and cluster names are recorded as their SHA-256, matching the convention
the merged Mosaic qualification receipt uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _kubectl(*arguments: str, check: bool = True) -> str:
    command = ["kubectl", *arguments]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def _json(*arguments: str) -> Any:
    return json.loads(_kubectl(*arguments, "-o", "json"))


def _hashed(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def apply_plan(plan: dict[str, Any]) -> None:
    payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": plan["manifests"]})
    process = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(process.stdout.strip())
    if process.returncode != 0:
        raise SystemExit("applying the plan failed")


def wait_for(namespace: str, job: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        status = _json("-n", namespace, "get", "job", job).get("status", {})
        if status.get("succeeded"):
            return "Succeeded"
        if status.get("failed"):
            return "Failed"
        state = f"active={status.get('active', 0)} ready={status.get('ready', 0)}"
        if state != last:
            print(f"  {job}: {state}", flush=True)
            last = state
        time.sleep(10)
    return "TimedOut"


def collect(namespace: str, job_name: str, variant: str) -> dict[str, Any]:
    job = _json("-n", namespace, "get", "job", job_name)
    pods = _json("-n", namespace, "get", "pod", "-l", f"job-name={job_name}").get("items", [])
    if not pods:
        return {"variant": variant, "job": job_name, "error": "no pod was created"}
    pod = pods[0]
    container = (pod["status"].get("containerStatuses") or [{}])[0]
    terminated = container.get("state", {}).get("terminated", {})
    node_name = pod["spec"].get("nodeName", "")

    node: dict[str, Any] = {}
    if node_name:
        raw = _json("get", "node", node_name)
        labels = raw["metadata"]["labels"]
        node = {
            # The plain instance name is an opaque Nebius resource ID and is
            # barred from the public export by tests/test_public_export.py.
            # The digest keeps the discriminating power -- two runs on the same
            # node still show the same value -- without publishing the name.
            "name_sha256": _hashed(node_name),
            "gpu_name": labels.get("nebius.com/gpu-name"),
            "accelerator_class": labels.get("accelerator.fs2.nebius/class"),
            "capacity_source": labels.get("capacity.fs2.nebius/source"),
            "instance_type": labels.get("node.kubernetes.io/instance-type"),
            "driver_version": labels.get("nebius.com/nvidia_driver_version"),
            "allocatable_gpu": raw["status"]["allocatable"].get("nvidia.com/gpu"),
        }

    events = _json(
        "-n", namespace, "get", "events",
        "--field-selector", f"involvedObject.name={pod['metadata']['name']}",
    ).get("items", [])
    image_events = [
        {"reason": item["reason"], "message": item["message"]}
        for item in events
        if item["reason"] in ("Pulling", "Pulled", "Scheduled", "Created", "Started")
    ]
    already_present = any(
        "already present on machine" in item["message"]
        for item in image_events
        if item["reason"] == "Pulled"
    )
    pulled_in_run = any(item["reason"] == "Pulling" for item in image_events)

    created = _stamp(job["metadata"].get("creationTimestamp"))
    started = _stamp(terminated.get("startedAt"))
    finished = _stamp(terminated.get("finishedAt"))

    def delta(first: datetime | None, second: datetime | None) -> float | None:
        if first and second:
            return round((second - first).total_seconds(), 3)
        return None

    return {
        "variant": variant,
        "job": job_name,
        "job_uid": job["metadata"]["uid"],
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "pod_name_sha256": _hashed(pod["metadata"]["name"]),
        "container_id": container.get("containerID"),
        "image": container.get("image"),
        "image_id": container.get("imageID"),
        "exit_code": terminated.get("exitCode"),
        "reason": terminated.get("reason"),
        "node": node,
        "timings": {
            "job_created_at": job["metadata"].get("creationTimestamp"),
            "container_started_at": terminated.get("startedAt"),
            "container_finished_at": terminated.get("finishedAt"),
            "schedule_to_container_start_seconds": delta(created, started),
            "container_runtime_seconds": delta(started, finished),
            "schedule_to_semantic_complete_seconds": delta(created, finished),
        },
        "image_phase": {
            "already_present_on_node": already_present,
            "pulled_during_this_run": pulled_in_run,
            "observed_cache_level": "image-local" if already_present and not pulled_in_run else "cold",
            "events": image_events,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(HERE / "generated-plan.json"))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--output", default=str(HERE / "submitted-runs.json"))
    parser.add_argument("--skip-apply", action="store_true")
    arguments = parser.parse_args()

    plan_path = Path(arguments.plan)
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    namespace = plan["namespace"]
    jobs = [
        (item["metadata"]["name"], item["metadata"]["labels"]["fs2.nebius.ai/variant"])
        for item in plan["manifests"]
        if item["kind"] == "Job"
    ]

    if not arguments.skip_apply:
        apply_plan(plan)

    outcomes = []
    for job_name, variant in jobs:
        print(f"waiting for {job_name} ({variant})", flush=True)
        phase = wait_for(namespace, job_name, arguments.timeout)
        record = collect(namespace, job_name, variant)
        record["job_phase"] = phase
        outcomes.append(record)
        print(
            f"  {variant}: phase={phase} exit={record.get('exit_code')} "
            f"schedule_to_complete="
            f"{record.get('timings', {}).get('schedule_to_semantic_complete_seconds')}s",
            flush=True,
        )

    receipt = {
        "schema": "fs2.nebius.ai/proteina-complexa-qualification-runs/v1",
        "owner_task": plan["owner_task"],
        "collected_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "namespace": namespace,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "image_reference": plan["rendered_from"]["image_reference"],
        "entrypoint_sha256": plan["rendered_from"]["entrypoint_sha256"],
        "runs": outcomes,
    }
    Path(arguments.output).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {arguments.output}")
    return 0 if all(item.get("exit_code") == 0 for item in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
