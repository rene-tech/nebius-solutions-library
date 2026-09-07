#!/usr/bin/env python3
"""Run the production RF snapshot transform on one original prepared request.

Only the test's input transport is local: the exact original working directory,
argv, stage completion writer, resources and production snapshot transform are
kept. No production model policy, workload or cloud resource is changed.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

from fs2_serve.scientific_batch.startup import (
    StageStartupPolicy,
    apply_startup_policy,
    canonical_bundle,
    validate_bundle,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("job", "bundle", "case", "directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("name", "node"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--force-unavailable-scratch",
        action="store_true",
        help="Probe ordinary fallback without changing the shared bundle",
    )
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    record = json.loads(args.bundle.read_bytes())
    validate_bundle(record, record["bundle_id"])
    assert record["qualified"] and record["model_id"] == "rfdiffusion"
    original = json.loads(args.job.read_bytes())
    spec = copy.deepcopy(original["spec"]["template"]["spec"])
    runtime = next(
        row for row in spec["containers"] if row["name"] == "scientific-stage"
    )
    assert runtime["command"] + runtime.get("args", []) == json.loads(
        (args.case / "original-command.json").read_bytes()
    )
    assert runtime["workingDir"] == json.loads(
        (args.case / "working-directory.json").read_bytes()
    )
    runtime["env"] = [
        row
        for row in runtime["env"]
        if "value" in row
        and not any(
            word in row["name"]
            for word in ("TOKEN", "CAPABILITY", "SECRET", "PASSWORD")
        )
    ]
    spec["containers"], spec["initContainers"] = [runtime], []
    spec.pop("schedulingGates", None)
    spec.pop("nodeName", None)
    spec["nodeSelector"] = {"kubernetes.io/hostname": args.node}
    spec["restartPolicy"] = "Never"
    spec["activeDeadlineSeconds"] = 900
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": args.name,
            "namespace": "fs2-models",
            "labels": {
                "snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907"
            },
        },
        "spec": spec,
    }
    policy = StageStartupPolicy(
        "cuda-criu", record["bundle_id"], canonical_bundle(record)
    )
    pod = apply_startup_policy(pod, policy, request_uid=10001)
    runtime = pod["spec"]["containers"][0]
    # Keep a successful test Pod available briefly for exact output retrieval;
    # this acceptance-only parent preserves the production command's exit code.
    command = runtime["command"]
    if args.force_unavailable_scratch:
        command[command.index("--source-directory") + 1] = (
            "/unavailable-scientific-snapshot"
        )
    runtime["command"] = [
        record["python"],
        "-c",
        "import subprocess,sys,time;code=subprocess.call(sys.argv[1:]);print('FS2_PRODUCTION_REQUEST_EXIT '+str(code),flush=True);time.sleep(120 if code==0 else 0);raise SystemExit(code)",
        *command,
    ]
    payload = (args.case / "prepared-workspace.tar").read_bytes()
    assert len(payload) < 900_000
    input_name = args.name + "-inputs"
    pod["spec"]["volumes"].append(
        {"name": "prepared-original", "configMap": {"name": input_name}}
    )
    workspace = next(
        copy.deepcopy(row)
        for row in runtime["volumeMounts"]
        if row["mountPath"] == "/mnt/fs2-scientific"
    )
    pod["spec"]["initContainers"].append(
        {
            "name": "original-prepared-input",
            "image": record["runtime_image"],
            "command": [
                "tar",
                "-C",
                "/mnt/fs2-scientific",
                "-xpf",
                "/prepared/original.tar",
            ],
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
            "volumeMounts": [
                workspace,
                {
                    "name": "prepared-original",
                    "mountPath": "/prepared",
                    "readOnly": True,
                },
            ],
        }
    )
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]

    def call(argv, document=None, check=True):
        return subprocess.run(
            [*kube, *argv],
            input=None if document is None else json.dumps(document),
            text=True,
            capture_output=True,
            check=check,
            timeout=180,
        )

    root = Path(__file__).resolve().parents[3]
    entrypoint = (
        root / "models/scientific-snapshot/scientific_request_entrypoint.py"
    ).read_bytes()
    assert hashlib.sha256(entrypoint).hexdigest() == record["entrypoint"]["sha256"]
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "immutable": True,
        "metadata": {
            "name": record["entrypoint"]["configmap"],
            "labels": pod["metadata"]["labels"],
        },
        "data": {
            record["entrypoint"]["key"]: entrypoint.decode(),
            record["cli_key"]: (
                root / "models/scientific-snapshot" / record["cli_key"]
            ).read_text(),
        },
    }
    existing = call(
        ["get", "configmap", cm["metadata"]["name"], "-o", "json"], check=False
    )
    if existing.returncode == 0:
        assert json.loads(existing.stdout)["data"] == cm["data"]
    else:
        call(["create", "--dry-run=client", "-f", "-"], cm)
        call(["create", "-f", "-"], cm)
    call(
        ["create", "-f", "-"],
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "immutable": True,
            "metadata": {"name": input_name, "labels": pod["metadata"]["labels"]},
            "binaryData": {"original.tar": base64.b64encode(payload).decode()},
        },
    )
    (args.directory / "pod-private.json").write_text(json.dumps(pod))
    receipt = {"status": "running", "bundle_id": record["bundle_id"]}
    try:
        call(["create", "--dry-run=client", "-f", "-"], pod)
        call(["create", "-f", "-"], pod)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            state = call(["get", "pod", args.name, "-o", "json"])
            logs = call(
                ["logs", args.name, "-c", "scientific-stage", "--timestamps"],
                check=False,
            )
            (args.directory / "pod-final-private.json").write_text(state.stdout)
            (args.directory / "scientific-stage.log").write_text(
                logs.stdout + logs.stderr
            )
            if "FS2_PRODUCTION_REQUEST_EXIT 0" in logs.stdout:
                expected_mechanism = (
                    "normal-load-fallback"
                    if args.force_unavailable_scratch
                    else "cuda-criu-restored"
                )
                if '"mechanism": "' + expected_mechanism + '"' not in logs.stdout:
                    raise ValueError(
                        "production invocation used an unexpected startup path"
                    )
                output = json.loads((args.case / "original-command.json").read_bytes())
                output = output[output.index("--output") + 1]
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
                        output,
                        "-cf",
                        "-",
                        ".",
                    ]
                )
                (args.directory / "outputs.tar").write_bytes(archive)
                from report_rfdiffusion_pairs import original_result

                with tarfile.open(args.directory / "outputs.tar") as output_archive:
                    stream = output_archive.extractfile("./result.json")
                    assert stream is not None
                    cache = json.load(stream)["cache_level"]
                assert cache["gpu_snapshot_used"] is (
                    not args.force_unavailable_scratch
                )
                assert cache["source"] == "runtime-observed"
                receipt.update(
                    status="passed",
                    result=original_result(args.directory / "outputs.tar"),
                    actual_startup=cache["observed_startup"],
                )
                break
            if json.loads(state.stdout)["status"]["phase"] in {"Failed", "Succeeded"}:
                raise ValueError(
                    "production request did not report successful restore and completion"
                )
            time.sleep(2)
        else:
            raise TimeoutError("production-transform probe did not complete")
    finally:
        deleted_pod = call(
            [
                "delete",
                "pod",
                args.name,
                "--wait=true",
                "--timeout="
                + str(max(90, int(pod["spec"].get("terminationGracePeriodSeconds", 30)) + 30))
                + "s",
                "--ignore-not-found",
            ],
            check=False,
        )
        deleted_inputs = call(
            ["delete", "configmap", input_name, "--wait=true", "--ignore-not-found"],
            check=False,
        )
        receipt["test_pod_and_inputs_deleted"] = (
            deleted_pod.returncode == deleted_inputs.returncode == 0
        )
        if receipt["status"] == "running":
            receipt["status"] = "failed"
        receipt["retained_entrypoint_configmap"] = record["entrypoint"]["configmap"]
        (args.directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
