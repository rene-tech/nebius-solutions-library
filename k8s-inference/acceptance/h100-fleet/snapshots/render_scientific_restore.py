#!/usr/bin/env python3
"""Render an optional scientific restore over an unchanged accepted Job.

The immutable shared bundle is read-only. Each Pod owns its writable cache,
temporary files and log at the original captured absolute paths. Original
model localization, request preparation, wrapper argv and collectors remain.
This is the tested integration fragment; API policy and Terraform own whether
it is selected, and must bind the exact qualified model/runtime/bundle tuple.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from fs2_serve.snapshot_metadata import snapshot_cache_copy_command

from render_serving_probe import render


def scientific_restore(source, config):
    original_spec = source["spec"]["template"]["spec"]
    original_runtime = next(
        item
        for item in original_spec["containers"]
        if item["name"] == "scientific-stage"
    )
    original_command = original_runtime["command"] + original_runtime.get("args", [])
    args = argparse.Namespace(
        container="scientific-stage",
        entrypoint_json="[]",
        asyncio_loop=False,
        python=config["python"],
        run=config["bundle_path"],
        fallback="normal-load",
        mode="restore",
        request_uid=10001,
        allow_device_remap=True,
        tools_image=config["tools_image"],
        model_revision=config["model_revision"],
        model_id=config["model_id"],
        source_configmap=config["source_configmap"],
        pvc=config["pvc"],
        name=config["name"],
        node=config.get("node", ""),
    )
    pod = render(source, args)
    spec = pod["spec"]
    if not config.get("node"):
        spec["nodeSelector"] = original_spec.get("nodeSelector", {})
    runtime = next(
        item for item in spec["containers"] if item["name"] == "scientific-stage"
    )
    directory = "/checkpoints/" + config["bundle_path"]
    runtime["command"] = [
        config["python"],
        "/snapshot-source/supervisor.py",
        "--directory",
        directory,
        "--source-directory",
        "/snapshot-bundle",
        "--fallback",
        "normal-load",
        "--allow-device-remap",
        "--request-uid",
        "10001",
        "--request-gid",
        "10001",
        "--worker-url-variable",
        config["worker_variable"],
        "restore",
        "--",
        *original_command,
    ]
    for volume in spec["volumes"]:
        if volume["name"] == "snapshot-checkpoints":
            volume.clear()
            volume.update({"name": "snapshot-checkpoints", "emptyDir": {}})
    spec["volumes"].extend(
        [
            {
                "name": "snapshot-bundle",
                "persistentVolumeClaim": {"claimName": config["pvc"], "readOnly": True},
            },
            {
                "name": "snapshot-cli",
                "configMap": {"name": config["cli_configmap"], "defaultMode": 365},
            },
        ]
    )
    bundle_mount = {
        "name": "snapshot-bundle",
        "mountPath": "/snapshot-bundle",
        "subPath": config["bundle_path"],
        "readOnly": True,
    }
    runtime["volumeMounts"].extend(
        [
            bundle_mount,
            {
                "name": "snapshot-bundle",
                "mountPath": directory + "/images",
                "subPath": config["bundle_path"] + "/images",
                "readOnly": True,
            },
            {
                "name": "snapshot-cli",
                "mountPath": config["cli_path"],
                "subPath": config["cli_key"],
                "readOnly": True,
            },
        ]
    )
    initializer = next(
        item for item in spec["initContainers"] if item["name"] == "snapshot-tools"
    )
    initializer["volumeMounts"].append(bundle_mount)
    initializer["command"][2] += " && " + snapshot_cache_copy_command()
    # The supervisor copies cache and worker.log from the source. Its cache
    # mkdir must not already exist when --source-directory is used.
    initializer["command"][2] = initializer["command"][2].replace('"$1/cache" ', "")
    if config.get("worker_log"):
        from fs2_serve.snapshot_metadata import SnapshotWorkerLog, prepare_worker_log_shadow

        prepare_worker_log_shadow(
            initializer, runtime, directory, SnapshotWorkerLog.model_validate(config["worker_log"])
        )
    return pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            scientific_restore(
                json.loads(args.source.read_bytes()),
                json.loads(args.config.read_bytes()),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
