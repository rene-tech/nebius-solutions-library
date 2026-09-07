#!/usr/bin/env python3
"""Reuse accepted controller preparation with freshly authorized input bytes.

The disposable CPU Pod runs the exact original prepare/verify/materialize init
stages. Only artifact transport is redirected to already authorized, digest-
verified bytes, using the existing startup acceptance materializer. Original
request manifests, runtime markers and argument contracts remain unchanged.
"""

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("jobs-file", "outputs", "kubeconfig", "directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("model", "node", "name", "target-pod", "target-container"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    solution = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(solution / "acceptance/scientific-fleet"))
    from run_acceptance import PublicApiClient

    bundle = json.loads(args.outputs.read_bytes())
    client = PublicApiClient(
        bundle["endpoints"]["inference_base_url"].removesuffix("/v1"),
        bundle["credentials"]["scientific_access_token"],
    )
    source = next(
        job
        for job in json.loads(args.jobs_file.read_bytes())["items"]
        if job["metadata"]["labels"].get("fs2.nebius.ai/model-id") == args.model
        and any(
            container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu")
            for container in job["spec"]["template"]["spec"]["containers"]
        )
    )
    spec = copy.deepcopy(source["spec"]["template"]["spec"])
    stage = next(
        container
        for container in spec["containers"]
        if container["name"] == "scientific-stage"
    )
    original = stage["command"] + (stage.get("args") or [])
    (args.directory / "original-command.json").write_text(json.dumps(original))
    # The ordinary wrapper must see its own operation/marker bindings, not
    # those of a previous input used with the same request-ready model.
    environment = {
        item["name"]: item["value"]
        for item in stage.get("env", [])
        if item["name"].startswith("FS2_")
        and "value" in item
        and not any(
            word in item["name"]
            for word in ("TOKEN", "CAPABILITY", "SECRET", "PASSWORD")
        )
    }
    (args.directory / "request-environment.json").write_text(json.dumps(environment))
    namespace = source["metadata"]["namespace"]
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "k8s-inference-h100",
        "-n",
        namespace,
    ]

    def call(arguments, document=None):
        return subprocess.run(
            [*kube, *arguments],
            input=None if document is None else json.dumps(document),
            text=True,
            capture_output=True,
            check=True,
        )

    binary_data, bindings = {}, []
    for init in spec["initContainers"]:
        command = init["command"]
        if command[1] not in {"scientific-materialize", "scientific-materialize-many"}:
            continue
        commands = (
            [command[2:]]
            if command[1] == "scientific-materialize"
            else json.loads(command[command.index("--commands-json") + 1])
        )
        for entry in commands:
            fields = dict(zip(entry[::2], entry[1::2]))
            artifact_id = fields["--artifact-id"]
            response = client.request("GET", f"/v1/artifacts/{artifact_id}/content")
            assert response.status == 200
            digest = hashlib.sha256(response.body).hexdigest()
            assert "sha256:" + digest == fields["--expected-digest"]
            assert len(response.body) == int(fields["--expected-size-bytes"])
            binary_data[artifact_id] = base64.b64encode(response.body).decode()
            bindings.append(
                {
                    "artifact_id": artifact_id,
                    "sha256": digest,
                    "bytes": len(response.body),
                }
            )
        stage["image"] = init["image"]
        init["command"] = ["python", "/benchmark/materialize.py", *command[1:]]
        for item in init.get("env", []):
            if item["name"] == "FS2_SCIENTIFIC_WORKLOAD_CAPABILITY":
                item["value"] = "benchmark-local-inputs-no-network-capability"
        init.setdefault("volumeMounts", []).extend(
            [
                {
                    "name": "snapshot-input-tools",
                    "mountPath": "/benchmark",
                    "readOnly": True,
                },
                {
                    "name": "snapshot-input-bytes",
                    "mountPath": "/benchmark-inputs",
                    "readOnly": True,
                },
            ]
        )
    assert binary_data and sum(len(value) for value in binary_data.values()) < 950000
    materializer = (
        solution
        / "acceptance/scientific-startup/current/structure_local_materialize.py"
    )
    for suffix, values in (
        ("tools", {"data": {"materialize.py": materializer.read_text()}}),
        ("bytes", {"binaryData": binary_data}),
    ):
        call(
            ["create", "-f", "-"],
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": args.name + "-" + suffix},
                **values,
            },
        )
    spec["volumes"].extend(
        [
            {
                "name": "snapshot-input-tools",
                "configMap": {"name": args.name + "-tools", "defaultMode": 292},
            },
            {
                "name": "snapshot-input-bytes",
                "configMap": {"name": args.name + "-bytes", "defaultMode": 292},
            },
        ]
    )
    stage["command"] = ["python", "-c", "import time;time.sleep(1200)"]
    stage.pop("args", None)
    stage["env"] = []
    stage["resources"] = {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "1", "memory": "512Mi"},
    }
    spec["containers"] = [stage]
    spec["restartPolicy"] = "Never"
    spec.pop("nodeName", None)
    spec.pop("schedulingGates", None)
    spec["nodeSelector"] = {"kubernetes.io/hostname": args.node}
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": args.name,
            "labels": {
                "snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907"
            },
        },
        "spec": spec,
    }
    (args.directory / "cpu-pod.json").write_text(json.dumps(pod, indent=2))
    call(["create", "-f", "-"], pod)
    try:
        deadline = time.monotonic() + 600
        while True:
            status = json.loads(call(["get", "pod", args.name, "-o", "json"]).stdout)
            phase = status.get("status", {}).get("phase")
            if phase == "Running":
                break
            if phase == "Failed" or time.monotonic() > deadline:
                raise RuntimeError(
                    "scientific CPU preparation did not finish; inspect retained task evidence"
                )
            time.sleep(2)
        archive = subprocess.check_output(
            [
                *kube,
                "exec",
                args.name,
                "-c",
                "scientific-stage",
                "--",
                "tar",
                "-C",
                "/mnt/fs2-scientific",
                "-cf",
                "-",
                ".",
            ]
        )
        (args.directory / "prepared-workspace.tar").write_bytes(archive)
        subprocess.run(
            [
                *kube,
                "exec",
                "-i",
                args.target_pod,
                "-c",
                args.target_container,
                "--",
                "tar",
                "-C",
                "/mnt/fs2-scientific",
                "-xpf",
                "-",
            ],
            input=archive,
            check=True,
        )
        receipt = {
            "passed": True,
            "source_job": source["metadata"]["name"],
            "target_pod": args.target_pod,
            "artifacts": bindings,
            "prepared_archive_sha256": hashlib.sha256(archive).hexdigest(),
        }
        (args.directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    finally:
        call(["delete", "pod", args.name, "--wait=false"])
        call(["delete", "configmap", args.name + "-tools", args.name + "-bytes"])


if __name__ == "__main__":
    main()
