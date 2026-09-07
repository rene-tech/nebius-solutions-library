#!/usr/bin/env python3
"""Measure exact current structure runtimes in task-owned, instrumented Jobs.

Clone one accepted GPU stage, retaining image, model mounts, resources, argv and
normal output validation. The controller's expired capability is not reused:
customer-authorized input bytes are verified locally and mounted read-only.
No output is uploaded to the completed source operation.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def parse_markers(log: str) -> list[dict]:
    """Read standalone JSON events, including those following native progress bars.

    An environment report may contain an escaped marker in nested stdout: that
    is not a complete event JSON document and must never become a measurement.
    """
    markers = []
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs-file", type=Path, required=True)
    parser.add_argument(
        "--model",
        required=True,
        choices=(
            "esmfold2",
            "esmfold2-fast",
            "protenix-v2",
            "alphafold3",
            "openfold3-openbind",
        ),
    )
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--suffix", default="a")
    parser.add_argument("--environment-collector", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.receipts.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.repository_root / "acceptance/scientific-fleet"))
    import run_acceptance as public

    bundle = json.loads(args.outputs.read_bytes())
    endpoint = bundle["endpoints"]["inference_base_url"].removesuffix("/v1")
    client = public.PublicApiClient(
        endpoint, bundle["credentials"]["scientific_access_token"]
    )
    candidates = json.loads(args.jobs_file.read_bytes())["items"]
    source = next(
        job
        for job in candidates
        if job["metadata"]["labels"].get("fs2.nebius.ai/model-id") == args.model
        and any(
            container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
            for container in job["spec"]["template"]["spec"]["containers"]
        )
    )
    namespace = source["metadata"]["namespace"]
    prefix = f"fs2-start-{args.model[:22]}-{args.suffix}"
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "k8s-inference-h100",
        "-n",
        namespace,
    ]

    def call(arguments, document=None, check=True):
        result = subprocess.run(
            [*kube, *arguments],
            input=None if document is None else json.dumps(document),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(f"kubectl {arguments[:2]} failed: {result.stderr[:500]}")
        return result

    def create(document):
        call(["create", "--dry-run=client", "-f", "-"], document)
        call(["create", "-f", "-"], document)

    pod_spec = copy.deepcopy(source["spec"]["template"]["spec"])
    stage = next(
        container
        for container in pod_spec["containers"]
        if container["name"] == "scientific-stage"
    )
    original_command = stage["command"] + (stage.get("args") or [])
    here = Path(__file__).resolve().parent
    tools_data = {
        "sitecustomize.py": (here / "structure_sitecustomize.py").read_text(),
        "supervise.py": (here / "structure_supervise.py").read_text(),
        "materialize.py": (here / "structure_local_materialize.py").read_text(),
        "command.json": json.dumps(original_command),
    }
    if args.environment_collector:
        tools_data["collect_environment.py"] = args.environment_collector.read_text()
    binary_data = {}
    bindings = []
    for container in pod_spec.get("initContainers", []):
        command = container["command"]
        if command[1] not in {"scientific-materialize", "scientific-materialize-many"}:
            continue
        if command[1] == "scientific-materialize":
            materializations = [command[2:]]
        else:
            materializations = []
            for index, arg in enumerate(command):
                if arg == "--commands-json":
                    materializations.extend(json.loads(command[index + 1]))
        for entry in materializations:
            fields = {
                entry[index]: entry[index + 1] for index in range(0, len(entry), 2)
            }
            artifact_id = fields["--artifact-id"]
            response = client.request("GET", f"/v1/artifacts/{artifact_id}/content")
            assert response.status == 200, "public artifact download failed"
            assert len(response.body) == int(fields["--expected-size-bytes"])
            assert (
                "sha256:" + hashlib.sha256(response.body).hexdigest()
                == fields["--expected-digest"]
            )
            binary_data[artifact_id] = base64.b64encode(response.body).decode()
            bindings.append(
                {
                    "artifact_id": artifact_id,
                    "sha256": hashlib.sha256(response.body).hexdigest(),
                    "bytes": len(response.body),
                }
            )
        container["command"] = ["python", "/benchmark/materialize.py", *command[1:]]
        for env in container.get("env", []):
            if env["name"] == "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY":
                env["value"] = "benchmark-local-inputs-no-network-capability"
        container.setdefault("volumeMounts", []).extend(
            [
                {
                    "name": "benchmark-tools",
                    "mountPath": "/benchmark",
                    "readOnly": True,
                },
                {
                    "name": "benchmark-inputs",
                    "mountPath": "/benchmark-inputs",
                    "readOnly": True,
                },
            ]
        )
    assert sum(len(value) for value in binary_data.values()) < 950000, (
        "use a task PVC for larger inputs"
    )
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
    pod_spec["volumes"].extend(
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
    stage["env"].extend(
        [
            {"name": "FS2_STARTUP_BENCHMARK_MODEL", "value": args.model},
            {"name": "PYTHONPATH", "value": "/benchmark"},
        ]
    )
    interpreter = (
        original_command[0]
        if Path(original_command[0]).name.startswith("python")
        else "python"
    )
    stage["command"] = [
        "/bin/bash",
        "-c",
        "set -e; if [ -r /opt/fs2/activate.sh ]; then source /opt/fs2/activate.sh; fi; exec "
        + interpreter
        + " /benchmark/supervise.py",
    ]
    stage.pop("args", None)
    pod_spec["containers"] = [stage]
    pod_spec["restartPolicy"] = "Never"
    queue = source["metadata"]["labels"].get(
        "kueue.x-k8s.io/queue-name", "inference-models"
    )
    outputs = []
    for repetition in range(1, args.repetitions + 1):
        name = f"{prefix}-r{repetition:02}"
        labels = {
            "kueue.x-k8s.io/queue-name": queue,
            "fs2.nebius.ai/model-id": args.model,
            "benchmark.fs2.nebius/run": "startup-20260907-structure",
        }
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 1800,
                "suspend": True,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": copy.deepcopy(pod_spec),
                },
            },
        }
        (args.receipts / f"{name}.manifest.json").write_text(
            json.dumps(job, indent=2) + "\n"
        )
        create(job)
        print(
            json.dumps(
                {
                    "job": name,
                    "model_id": args.model,
                    "repetition": repetition,
                    "state": "submitted",
                }
            ),
            flush=True,
        )
        deadline = time.monotonic() + 1900
        pod = None
        while time.monotonic() < deadline:
            value = json.loads(
                call(["get", "pods", "-l", f"job-name={name}", "-o", "json"]).stdout
            )
            if value["items"]:
                pod = value["items"][0]
                (args.receipts / f"{name}.pod.json").write_text(
                    json.dumps(pod, indent=2) + "\n"
                )
                log = call(
                    [
                        "logs",
                        pod["metadata"]["name"],
                        "-c",
                        "scientific-stage",
                        "--timestamps",
                    ],
                    check=False,
                )
                if log.returncode == 0:
                    (args.receipts / f"{name}.log").write_text(log.stdout)
                if pod["status"]["phase"] in {"Succeeded", "Failed"}:
                    break
            time.sleep(3)
        if pod is None or pod["status"]["phase"] != "Succeeded":
            raise RuntimeError(f"isolated benchmark did not succeed: {name}")
        markers = parse_markers(log.stdout)
        ready = next(item for item in markers if item["phase"] == "model_ready")
        semantic = next(
            item for item in markers if item["phase"] == "semantic_output_verified"
        )
        record = {
            "job": name,
            "model_id": args.model,
            "repetition": repetition,
            "image": stage["image"],
            "original_command": original_command,
            "inputs": bindings,
            "pod_created_at": pod["metadata"]["creationTimestamp"],
            "container_started_at": pod["status"]["containerStatuses"][0]["state"][
                "terminated"
            ]["startedAt"],
            "ready": ready,
            "semantic_output": semantic,
            "markers": markers,
            "cache": "fresh process; unchanged existing model/reference cache; no eviction",
            "status": "passed",
        }
        (args.receipts / f"{name}.receipt.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        outputs.append(record)
        print(json.dumps({"job": name, "state": "passed", "ready": ready}), flush=True)
        call(["delete", "job", name, "--wait=true"])
    call(["delete", "configmap", prefix + "-tools", prefix + "-inputs"])
    (args.receipts / "isolated-summary.json").write_text(
        json.dumps(outputs, indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
