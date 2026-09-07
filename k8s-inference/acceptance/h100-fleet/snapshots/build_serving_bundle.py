#!/usr/bin/env python3
"""Bind completed matched evidence to the production serving snapshot schema."""

import argparse
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath


DEFAULT_FALLBACK_PREFIX = ["python3", "/snapshot-source/serving_launcher.py"]
DEFAULT_SUPERVISOR_PATH = "/tools/usr/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def captured_interpreters(runtime, restored=None):
    """Publish only measured non-default wrapper interpreters/environment."""
    values = {
        "supervisor_python": runtime["command"][0],
        "supervisor_path": next(item["value"] for item in runtime["env"] if item["name"] == "PATH"),
        "address_python": "python3",
    }
    if restored is not None:
        restored_runtime = next(item for item in restored["spec"]["containers"] if item["name"] == runtime["name"])
        if restored_runtime["image"] != runtime["image"]:
            raise ValueError("restore interpreter proof differs from captured image")
        values["address_python"] = next(item for item in restored["spec"]["initContainers"]
                                        if item["name"] == "snapshot-local-address")["command"][0]
    defaults = {"supervisor_python": "python3", "supervisor_path": DEFAULT_SUPERVISOR_PATH, "address_python": "python3"}
    return {key: value for key, value in values.items() if value != defaults[key]}


def unwrap_captured_command(command: list[str]) -> tuple[list[str], list[str]]:
    """Return the original image command and its measured fallback launcher."""
    original = command[command.index("--") + 1 :]
    if original[:2] == DEFAULT_FALLBACK_PREFIX:
        native = original[2:]
        prefix = DEFAULT_FALLBACK_PREFIX
    elif original[:2] == [
        "python3",
        "/snapshot-source/working_directory_launcher.py",
    ]:
        prefix = original[:9]
        if (
            len(prefix) != 9
            or prefix[2] != "--directory"
            or not PurePosixPath(prefix[3]).is_absolute()
            or ".." in PurePosixPath(prefix[3]).parts
            or prefix[4] != "--uid"
            or not prefix[5].isdigit()
            or int(prefix[5]) <= 0
            or prefix[6] != "--gid"
            or not prefix[7].isdigit()
            or int(prefix[7]) <= 0
            or prefix[8] != "--"
        ):
            raise ValueError("captured working-directory launcher differs")
        native = original[9:]
    else:
        raise ValueError("captured runtime must use a qualified serving launcher")
    if not native:
        raise ValueError("captured runtime must retain the original server command")
    return native, prefix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "report",
        "donor",
        "deployment",
        "model-deployment",
        "entrypoint",
        "output",
    ):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument(
        "--deployment-container",
        help="Production Deployment container name when it differs from the captured donor",
    )
    parser.add_argument("--restore", type=Path, help="Actual qualified restore Pod receipt for image-specific interpreter binding")
    parser.add_argument("--accelerator-class", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--source-configmap",
        default="fs2-fleet-snapshot-serving-v7",
    )
    args = parser.parse_args()
    report = json.loads(args.report.read_bytes())
    if report["status"] != "fresh-pod-qualified":
        raise ValueError("completed matched restore qualification is required")
    donor = json.loads(args.donor.read_bytes())
    native = json.loads(args.deployment.read_bytes())
    model = json.loads(args.model_deployment.read_bytes())["spec"]
    runtime = next(
        item for item in donor["spec"]["containers"] if item["name"] == args.container
    )
    source = next(
        item
        for item in native["spec"]["template"]["spec"]["containers"]
        if item["name"] == (args.deployment_container or args.container)
    )
    original, fallback_command_prefix = unwrap_captured_command(runtime["command"])
    image_entrypoint = (
        source.get("command") or original[: len(original) - len(source.get("args", []))]
    )
    if image_entrypoint + source.get("args", []) != original:
        raise ValueError("captured and original production argv differ")
    identity = report["compatibility"]["runtime_identity"]
    if (
        model["runtime"]["image"] != identity["runtime_image"]
        or model["artifact"]["revision"] != identity["model_revision"]
    ):
        raise ValueError("current ModelDeployment differs from measured runtime")
    directory = runtime["command"][runtime["command"].index("--directory") + 1]
    bundle = {
        "schema": "fs2-serve.nebius.ai/serving-snapshot-bundle/v1",
        "bundle_id": args.bundle_id,
        "model_ref": report["model_id"],
        "runtime_image": identity["runtime_image"],
        "model_revision": identity["model_revision"],
        "artifact_manifest_digest": model["artifact"]["manifestDigest"],
        "manifest_sha256": report["bundle"]["manifest_sha256"],
        "qualification_receipt_sha256": hashlib.sha256(
            args.report.read_bytes()
        ).hexdigest(),
        "qualified": True,
        "accelerator_classes": [args.accelerator_class],
        "compatibility": {
            key: identity[key]
            for key in (
                "gpu_name",
                "compute_capability",
                "driver_version",
                "kernel_release",
            )
        },
        "tools_image": identity["tools_image"],
        "source_configmap": args.source_configmap,
        "source_sha256": identity["snapshot_source_sha256"],
        "entrypoint_configmap": "fs2-fleet-snapshot-serving-entrypoint-v1",
        "entrypoint_sha256": hashlib.sha256(args.entrypoint.read_bytes()).hexdigest(),
        "network_configmap": "fs2-fleet-snapshot-net-tools-v1",
        "address_configmap": "fs2-fleet-snapshot-address-v1",
        "pvc": "fs2-fleet-snapshots-rwx-r20260907",
        "bundle_path": directory.removeprefix("/checkpoints/"),
        "captured_pod_ip": donor["status"]["podIP"],
        "image_entrypoint": image_entrypoint,
        "runtime_command": original,
        "fallback_command_prefix": fallback_command_prefix,
    }
    bundle.update(captured_interpreters(runtime, json.loads(args.restore.read_bytes()) if args.restore else None))
    args.output.write_text(json.dumps(bundle, indent=2) + "\n")


if __name__ == "__main__":
    main()
