#!/usr/bin/env python3
"""Measure primary scientific-model readiness in isolated, exact-image GPU Jobs.

One accepted public Job is cloned without its output uploader. Runtime image,
model mounts, resources, argv, precision and work size stay unchanged. The
expired per-attempt capability is never reused: authorized input bytes are
downloaded on the host, digest-verified and mounted read-only for the unchanged
materializer. Model-specific hook/supervisor files allow the same orchestration
to cover a stage whose native boundary differs from Mosaic and BindCraft. All
cluster objects are task-owned and removed after the run.
"""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


def load_jobs(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    sources = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for source in sources:
        value = json.loads(source.read_text(encoding="utf-8"))
        if value.get("kind") == "List":
            values.extend(value.get("items", []))
        elif value.get("kind") == "Job":
            values.append(value)
    return values


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds(start: str, finish: str) -> float:
    return round((parse_utc(finish) - parse_utc(start)).total_seconds(), 6)


def private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def private_json(path: Path, value: Any) -> None:
    private_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def materializations(command: list[str]) -> list[list[str]]:
    if len(command) < 2 or command[1] not in {
        "scientific-materialize",
        "scientific-materialize-many",
    }:
        return []
    if command[1] == "scientific-materialize":
        return [command[2:]]
    for index, argument in enumerate(command):
        if argument == "--commands-json" and index + 1 < len(command):
            value = json.loads(command[index + 1])
            if not isinstance(value, list):
                raise ValueError("materialize-many commands are not a list")
            return value
    raise ValueError("materialize-many command has no commands JSON")


def fields(arguments: list[str]) -> dict[str, str]:
    if len(arguments) % 2:
        raise ValueError("materializer arguments are not flag/value pairs")
    return {arguments[index]: arguments[index + 1] for index in range(0, len(arguments), 2)}


def parse_markers(log: str) -> list[dict[str, Any]]:
    """Accept only complete marker JSON, even beside native progress output."""
    markers: list[dict[str, Any]] = []
    for line in log.splitlines():
        _, separator, payload = line.partition("FS2_STARTUP ")
        if not separator:
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(
            key in value for key in ("phase", "model_id", "utc", "monotonic_seconds")
        ):
            markers.append(value)
    return markers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-file", type=Path, required=True)
    parser.add_argument("--model", choices=("bindcraft", "boltzgen", "mosaic"), required=True)
    parser.add_argument("--stage", default="design")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--hook-file", type=Path)
    parser.add_argument("--supervisor-file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.receipts.mkdir(parents=True, exist_ok=False, mode=0o700)

    sys.path.insert(0, str(args.repository_root / "acceptance/scientific-fleet"))
    import run_acceptance as public

    bundle = json.loads(args.outputs.read_text(encoding="utf-8"))
    endpoint = bundle["endpoints"]["inference_base_url"].removesuffix("/v1")
    client = public.PublicApiClient(
        endpoint, bundle["credentials"]["scientific_access_token"]
    )
    candidates = load_jobs(args.jobs_file)
    source = next(
        job
        for job in candidates
        if job.get("metadata", {}).get("labels", {}).get("fs2.nebius.ai/model-id")
        == args.model
        and job.get("metadata", {}).get("labels", {}).get("fs2.nebius.ai/stage-id")
        == args.stage
        and any(
            container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
            for container in job["spec"]["template"]["spec"].get("containers", [])
        )
    )
    namespace = source["metadata"]["namespace"]
    prefix = f"fs2-start-{args.model[:18]}-{args.suffix}"
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
        "-n",
        namespace,
    ]

    def call(values: list[str], document: dict[str, Any] | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        response = subprocess.run(
            [*kube, *values],
            input=None if document is None else json.dumps(document),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and response.returncode:
            raise RuntimeError(
                f"kubectl {values[:2]} failed ({response.returncode}): "
                f"{response.stderr[:500]}"
            )
        return response

    def create(document: dict[str, Any]) -> None:
        call(["create", "--dry-run=client", "-f", "-"], document)
        call(["create", "-f", "-"], document)

    pod_spec = copy.deepcopy(source["spec"]["template"]["spec"])
    stage = next(
        container
        for container in pod_spec["containers"]
        if container["name"] == "scientific-stage"
    )
    original_command = [*stage["command"], *(stage.get("args") or [])]
    here = Path(__file__).resolve().parent
    hook_file = args.hook_file or here / "primary_sitecustomize.py"
    supervisor_file = args.supervisor_file or here / "primary_supervise.py"
    tools_data = {
        "sitecustomize.py": hook_file.read_text(encoding="utf-8"),
        "supervise.py": supervisor_file.read_text(encoding="utf-8"),
        "materialize.py": (here / "primary_local_materialize.py").read_text(encoding="utf-8"),
        "command.json": json.dumps(original_command, separators=(",", ":")),
    }
    binary_data: dict[str, str] = {}
    bindings: list[dict[str, Any]] = []
    for container in pod_spec.get("initContainers", []):
        command = container.get("command") or []
        entries = materializations(command)
        for entry in entries:
            parsed = fields(entry)
            artifact_id = parsed["--artifact-id"]
            response = client.request("GET", f"/v1/artifacts/{artifact_id}/content")
            if response.status != 200:
                raise RuntimeError("public artifact download failed")
            digest = hashlib.sha256(response.body).hexdigest()
            if (
                len(response.body) != int(parsed["--expected-size-bytes"])
                or "sha256:" + digest != parsed["--expected-digest"]
            ):
                raise RuntimeError("downloaded input differs from its frozen pointer")
            binary_data[artifact_id] = base64.b64encode(response.body).decode()
            bindings.append(
                {"artifact_id": artifact_id, "sha256": digest, "bytes": len(response.body)}
            )
        if entries:
            container["command"] = ["python", "/benchmark/materialize.py", *command[1:]]
            container.setdefault("volumeMounts", []).extend(
                [
                    {"name": "benchmark-tools", "mountPath": "/benchmark", "readOnly": True},
                    {"name": "benchmark-inputs", "mountPath": "/benchmark-inputs", "readOnly": True},
                ]
            )
        for environment in container.get("env", []):
            if environment.get("name") == "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY":
                environment.clear()
                environment.update(
                    {"name": "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY", "value": "benchmark-local-no-capability"}
                )
    if sum(len(value) for value in binary_data.values()) >= 950_000:
        raise RuntimeError("task input is too large for an isolated ConfigMap")

    create(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": prefix + "-tools"},
            "data": tools_data,
        }
    )
    create(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": prefix + "-inputs"},
            "binaryData": binary_data,
        }
    )
    pod_spec.setdefault("volumes", []).extend(
        [
            {
                "name": "benchmark-tools",
                "configMap": {"name": prefix + "-tools", "defaultMode": 292},
            },
            {
                "name": "benchmark-inputs",
                "configMap": {"name": prefix + "-inputs", "defaultMode": 292},
            },
        ]
    )
    stage.setdefault("volumeMounts", []).append(
        {"name": "benchmark-tools", "mountPath": "/benchmark", "readOnly": True}
    )
    environment = stage.setdefault("env", [])
    python_path = next(
        (item for item in environment if item.get("name") == "PYTHONPATH"), None
    )
    inherited_python_path = python_path.get("value", "") if python_path else ""
    if python_path is None:
        python_path = {"name": "PYTHONPATH", "value": ""}
        environment.append(python_path)
    # BindCraft's unchanged runtime gate requires its academic PyRosetta tree
    # to remain the first PYTHONPATH entry.  sitecustomize is still discovered
    # from /benchmark when it is appended to that exact canonical prefix.
    python_path["value"] = (
        inherited_python_path + ":/benchmark"
        if inherited_python_path
        else "/benchmark"
    )
    environment.append({"name": "FS2_STARTUP_BENCHMARK_MODEL", "value": args.model})
    for item in environment:
        if item.get("name") == "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY":
            item.clear()
            item.update(
                {"name": "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY", "value": "benchmark-local-no-capability"}
            )
    stage["command"] = [
        "/bin/bash",
        "-c",
        "set -e; if [ -r /opt/fs2/activate.sh ]; then source /opt/fs2/activate.sh; fi; "
        "exec python /benchmark/supervise.py",
    ]
    stage.pop("args", None)
    pod_spec["containers"] = [stage]
    pod_spec["restartPolicy"] = "Never"
    pod_spec.pop("schedulingGates", None)
    pod_spec.pop("nodeName", None)

    queue = source["metadata"]["labels"].get(
        "kueue.x-k8s.io/queue-name", "inference-models"
    )
    source_deadline = int(source.get("spec", {}).get("activeDeadlineSeconds", 43_200))
    outputs: list[dict[str, Any]] = []
    try:
        for repetition in range(1, args.repetitions + 1):
            name = f"{prefix}-r{repetition:02}"
            labels = {
                "kueue.x-k8s.io/queue-name": queue,
                "fs2.nebius.ai/model-id": args.model,
                "benchmark.fs2.nebius/run": "startup-primary-20260907",
            }
            job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": name, "labels": labels},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": source_deadline,
                    "suspend": True,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": copy.deepcopy(pod_spec),
                    },
                },
            }
            private_json(args.receipts / f"{name}.manifest.json", job)
            create(job)
            print(
                json.dumps(
                    {"job": name, "model_id": args.model, "repetition": repetition, "state": "submitted"}
                ),
                flush=True,
            )
            deadline = time.monotonic() + source_deadline + 120
            pod: dict[str, Any] | None = None
            log = ""
            while time.monotonic() < deadline:
                response = call(
                    ["get", "pods", "-l", f"job-name={name}", "-o", "json"]
                )
                items = json.loads(response.stdout).get("items", [])
                if items:
                    pod = items[0]
                    private_json(args.receipts / f"{name}.pod.json", pod)
                    for container_name in [
                        *(
                            item.get("name")
                            for item in pod.get("spec", {}).get("initContainers", [])
                        ),
                        "scientific-stage",
                    ]:
                        if not container_name:
                            continue
                        captured = call(
                            ["logs", pod["metadata"]["name"], "-c", container_name, "--timestamps"],
                            check=False,
                        )
                        if captured.returncode == 0 and captured.stdout:
                            private_text(
                                args.receipts / f"{name}.{container_name}.log",
                                captured.stdout,
                            )
                    logs = call(
                        ["logs", pod["metadata"]["name"], "-c", "scientific-stage", "--timestamps"],
                        check=False,
                    )
                    if logs.returncode == 0:
                        log = logs.stdout
                        private_text(args.receipts / f"{name}.log", log)
                    if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                        break
                time.sleep(2)
            if pod is None or pod.get("status", {}).get("phase") != "Succeeded":
                raise RuntimeError(f"isolated benchmark did not succeed: {name}")
            uid = pod["metadata"]["uid"]
            events_response = call(
                [
                    "get",
                    "events",
                    "--field-selector",
                    f"involvedObject.uid={uid}",
                    "-o",
                    "json",
                ],
                check=False,
            )
            events = (
                json.loads(events_response.stdout).get("items", [])
                if events_response.returncode == 0
                else []
            )
            private_json(args.receipts / f"{name}.events.json", events)
            markers = parse_markers(log)
            ready = next(item for item in markers if item.get("phase") == "model_ready")
            semantic = next(
                item for item in markers if item.get("phase") == "semantic_output_verified"
            )
            stage_status = next(
                item
                for item in pod["status"]["containerStatuses"]
                if item["name"] == "scientific-stage"
            )
            state = stage_status["state"]["terminated"]
            container_started = state["startedAt"]
            ready_utc = ready["utc"]
            init_phases = [
                {
                    "name": item["name"],
                    "started_at": item.get("state", {}).get("terminated", {}).get("startedAt"),
                    "finished_at": item.get("state", {}).get("terminated", {}).get("finishedAt"),
                }
                for item in pod.get("status", {}).get("initContainerStatuses", [])
            ]
            pull_events = [
                {
                    "reason": item.get("reason"),
                    "message": item.get("message"),
                    "event_time": item.get("eventTime") or item.get("lastTimestamp"),
                }
                for item in events
                if item.get("reason") in {"Pulling", "Pulled", "BackOff"}
            ]
            record = {
                "schema": "fs2-serve.nebius.ai/primary-isolated-startup-receipt/v1",
                "job": name,
                "model_id": args.model,
                "repetition": repetition,
                "source_operation_id": source["metadata"]["labels"].get("fs2.nebius.ai/operation-id"),
                "source_variant_id": source["metadata"]["labels"].get("fs2.nebius.ai/variant-id"),
                "image": stage["image"],
                "image_id": stage_status.get("imageID"),
                "original_command_sha256": hashlib.sha256(
                    json.dumps(original_command, separators=(",", ":")).encode()
                ).hexdigest(),
                "inputs": bindings,
                "pod_created_at": pod["metadata"]["creationTimestamp"],
                "pod_started_at": pod["status"].get("startTime"),
                "container_started_at": container_started,
                "ready": ready,
                "semantic_output": semantic,
                "markers": markers,
                "init_phases": init_phases,
                "image_events": pull_events,
                "timings_seconds": {
                    "pod_create_to_container_start": seconds(
                        pod["metadata"]["creationTimestamp"], container_started
                    ),
                    "container_start_to_model_ready": seconds(container_started, ready_utc),
                    "pod_create_to_model_ready": seconds(
                        pod["metadata"]["creationTimestamp"], ready_utc
                    ),
                    "python_process_to_model_ready": ready.get("python_process_seconds"),
                    "container_start_to_valid_output": seconds(
                        container_started, semantic["utc"]
                    ),
                    "pod_create_to_valid_output": seconds(
                        pod["metadata"]["creationTimestamp"], semantic["utc"]
                    ),
                },
                "cache_condition": "fresh process; current image/node and immutable reference-data caches unchanged; no eviction",
                "status": "passed",
            }
            private_json(args.receipts / f"{name}.receipt.json", record)
            outputs.append(record)
            print(
                json.dumps(
                    {
                        "job": name,
                        "state": "passed",
                        "timings_seconds": record["timings_seconds"],
                    }
                ),
                flush=True,
            )
            call(["delete", "job", name, "--wait=true"])
    finally:
        for repetition in range(1, args.repetitions + 1):
            call(
                ["delete", "job", f"{prefix}-r{repetition:02}", "--ignore-not-found", "--wait=true"],
                check=False,
            )
        call(
            ["delete", "configmap", prefix + "-tools", prefix + "-inputs", "--ignore-not-found"],
            check=False,
        )
    private_json(args.receipts / "isolated-summary.json", outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
