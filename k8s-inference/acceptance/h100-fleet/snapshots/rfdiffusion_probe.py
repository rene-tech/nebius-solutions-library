#!/usr/bin/env python3
"""Isolated model-only RF donor; preserves original image/checkpoint/resources."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from render_serving_probe import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("job", "directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("name", "source-configmap", "pvc", "bundle-path"):
        parser.add_argument("--" + name, required=True)
    placement = parser.add_mutually_exclusive_group(required=True)
    placement.add_argument("--node")
    placement.add_argument("--pool")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    sources = root / "models/scientific-snapshot"
    data = {name: (sources / name).read_text() for name in (
        "supervisor.py", "process_checkpoint.py", "scientific_server.py", "rfdiffusion_server.py",
        "rfdiffusion_model_cache.py", "rfdiffusion_cli_proxy.py",
    )}
    data["sitecustomize.py"] = (sources / "python310_sitecustomize.py").read_text()
    data["rfdiffusion_runtime_entrypoint.py"] = (
        root / "models/cancer-immunotherapy/runtime-images/rfdiffusion/runtime_entrypoint.py"
    ).read_text()
    source = json.loads(args.job.read_bytes())
    assert source["metadata"]["labels"]["fs2.nebius.ai/model-id"] == "rfdiffusion"
    spec = source["spec"]["template"]["spec"]
    stage = next(item for item in spec["containers"] if item["name"] == "scientific-stage")
    original = stage["command"] + stage.get("args", [])
    (args.directory / "original-command.json").write_text(json.dumps(original))
    spec["containers"], spec["initContainers"] = [stage], []
    python = "/opt/conda/bin/python"
    stage["command"] = [python, "-u", "/snapshot-source/rfdiffusion_server.py"]
    stage.pop("args", None)
    environment = {row["name"]: row["value"] for row in stage.get("env", []) if "value" in row
                   and not any(word in row["name"] for word in ("TOKEN", "SECRET", "CAPABILITY", "PASSWORD"))}
    environment.update({"PYTHONPATH": "/snapshot-source:/opt/rfdiffusion", "DGLBACKEND": "pytorch",
        "FS2_RFDIFFUSION_CHECKPOINT": "/opt/fs2/artifacts/rfdiffusion-base-checkpoint/Base_ckpt.pt"})
    stage["env"] = [{"name": key, "value": value} for key, value in environment.items()]
    template = json.loads((Path(__file__).with_name("protenix-v2-bundle.json")).read_bytes())
    options = argparse.Namespace(container="scientific-stage", entrypoint_json="[]", asyncio_loop=False,
        python=python, run=args.bundle_path, fallback="fail", mode="donor", request_uid=10001,
        allow_device_remap=True, tools_image=template["tools_image"], model_id="rfdiffusion",
        model_revision="9273ef67335acaf91df0150473a274759229cdf6", source_configmap=args.source_configmap,
        pvc=args.pvc, name=args.name, node=args.node)
    pod = render(source, options)
    if args.pool:
        pod["spec"]["nodeSelector"].pop("kubernetes.io/hostname", None)
        pod["spec"]["nodeSelector"]["accelerator.fs2.nebius/pool-id"] = args.pool
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "scientific-stage")
    # This model-only donor runs as the supervisor's root UID, so capture does
    # not need cross-UID prlimit capability. Inheriting it into a root worker
    # would make its captured capabilities differ from the normal restore set.
    runtime["securityContext"]["capabilities"]["add"].remove("SYS_RESOURCE")
    runtime["volumeMounts"].append({"name": "snapshot-source", "mountPath": "/opt/fs2/runtime_entrypoint.py",
                                   "subPath": "rfdiffusion_cli_proxy.py", "readOnly": True})
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {"name": args.source_configmap, "namespace": "fs2-models",
            "labels": {"snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907"}}, "data": data}
    (args.directory / "source-worker-private.json").write_text(json.dumps(source))
    (args.directory / "donor-private.json").write_text(json.dumps(pod))
    (args.directory / "source-sha256.json").write_text(json.dumps(
        {key: hashlib.sha256(value.encode()).hexdigest() for key, value in data.items()}, indent=2))
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]
    existing = subprocess.run([*kube, "get", "configmap", args.source_configmap, "-o", "json"], capture_output=True, text=True)
    if existing.returncode == 0:
        assert json.loads(existing.stdout)["data"] == data, "immutable source differs; choose a new version"
    else:
        subprocess.run([*kube, "create", "-f", "-"], input=json.dumps(configmap), text=True, check=True)
    subprocess.run([*kube, "create", "--dry-run=client", "-f", "-"], input=json.dumps(pod), text=True, check=True)
    subprocess.run([*kube, "create", "-f", "-"], input=json.dumps(pod), text=True, check=True)


if __name__ == "__main__":
    main()
