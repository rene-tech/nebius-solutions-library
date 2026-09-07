#!/usr/bin/env python3
"""Render task-owned snapshot probes from exact current serving specifications.

The serving argv, images, model mounts and resource limits are retained. Only
the optional supervisor, snapshot tools and durable runtime scratch are added.
Source specs and rendered manifests are private operator inputs, not evidence
to commit. Kubernetes continues to allocate the GPU normally.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

TASK = "fs2-h100-fleet-snapshot-options-r20260907"


def render(source: dict, args: argparse.Namespace) -> dict:
    spec = copy.deepcopy(source["spec"]["template"]["spec"])
    security = spec.setdefault("securityContext", {})
    group = security.pop("fsGroup", None)
    security.pop("fsGroupChangePolicy", None)
    if group is not None:
        security["supplementalGroups"] = sorted({*security.get("supplementalGroups", []), group})
    runtime = next(
        item for item in spec["containers"] if item["name"] == args.container
    )
    original = (runtime.get("command") or json.loads(args.entrypoint_json)) + (
        runtime.get("args") or []
    )
    if args.asyncio_loop:
        original = [args.python, "/snapshot-source/serving_launcher.py", *original]
    directory = "/checkpoints/" + args.run
    runtime["command"] = [
        args.python,
        "/snapshot-source/supervisor.py",
        "--directory",
        directory,
        "--request-mode",
        "server",
        "--fallback",
        args.fallback,
        *(
            [
                "--request-uid",
                str(args.request_uid),
                "--request-gid",
                str(args.request_uid),
            ]
            if args.request_uid is not None
            else []
        ),
        *(["--allow-device-remap"] if args.allow_device_remap else []),
        args.mode,
        "--",
        *original,
    ]
    runtime.pop("args", None)
    capabilities = [
        "SYS_ADMIN",
        "SYS_PTRACE",
        "CHECKPOINT_RESTORE",
        "NET_ADMIN",
        "SYS_TIME",
    ]
    if args.mode == "donor":
        # CRIU reads every worker rlimit while dumping. A root supervisor
        # capturing the production uid/gid worker needs CAP_SYS_RESOURCE for
        # that cross-uid prlimit operation. Restore never needs this capability.
        capabilities.append("SYS_RESOURCE")
    runtime["securityContext"] = {
        "runAsUser": 0,
        "runAsGroup": 0,
        "runAsNonRoot": False,
        "capabilities": {"add": capabilities},
        "seccompProfile": {"type": "Unconfined"},
        "appArmorProfile": {"type": "Unconfined"},
    }
    # The parent waits on the original HTTP readiness; capture intentionally
    # removes the worker and must not trigger a kubelet restart of the donor.
    for name in ("livenessProbe", "readinessProbe", "startupProbe", "lifecycle"):
        runtime.pop(name, None)
    runtime.setdefault("env", []).extend(
        [
            {"name": "FS2_SNAPSHOT_RUNTIME_IMAGE", "value": runtime["image"]},
            {"name": "FS2_SNAPSHOT_TOOLS_IMAGE", "value": args.tools_image},
            {"name": "FS2_MODEL_REVISION", "value": args.model_revision},
            {"name": "FS2_RUNTIME_ID", "value": args.model_id},
        ]
    )
    runtime.setdefault("volumeMounts", []).extend(
        [
            {"name": "snapshot-tools", "mountPath": "/tools"},
            {
                "name": "snapshot-source",
                "mountPath": "/snapshot-source",
                "readOnly": True,
            },
            {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
        ]
    )
    # Existing executable caches and Unix-socket paths must survive the donor.
    # Keep their original mount points; do not change the model's argv or cache
    # keys. Other containers retain their ordinary independent temporary dirs.
    if not any(mount["mountPath"] == "/tmp" for mount in runtime["volumeMounts"]):
        runtime["volumeMounts"].append(
            {"name": "snapshot-checkpoints", "mountPath": "/tmp"}
        )
    for mount in runtime["volumeMounts"]:
        if mount["mountPath"] in ("/runtime-cache", "/cache", "/tmp"):
            mount["name"] = "snapshot-checkpoints"
            mount["subPath"] = (
                args.run
                + "/"
                + ("tmp" if mount["mountPath"] == "/tmp" else "runtime-cache")
            )
    spec.setdefault("volumes", []).extend(
        [
            {"name": "snapshot-tools", "emptyDir": {}},
            {
                "name": "snapshot-source",
                "configMap": {"name": args.source_configmap, "defaultMode": 292},
            },
            {
                "name": "snapshot-checkpoints",
                "persistentVolumeClaim": {"claimName": args.pvc},
            },
        ]
    )
    spec.setdefault("initContainers", []).insert(
        0,
        {
            "name": "snapshot-tools",
            "image": args.tools_image,
            "command": [
                "/bin/sh",
                "-c",
                (
                    "cp -a /snapshot-binaries/. /tools/ && "
                    "cp -L /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 "
                    "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/ && "
                    'mkdir -p "$1/cache" "$1/runtime-cache" "$1/tmp" && '
                    'if [ -n "$2" ]; then chown "$2:$2" "$1/runtime-cache" "$1/tmp"; fi'
                ),
                "snapshot-tools",
                directory,
                str(args.request_uid) if args.request_uid is not None else "",
            ],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
            "volumeMounts": [
                {"name": "snapshot-tools", "mountPath": "/tools"},
                {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
            ],
        },
    )
    spec["restartPolicy"] = "Never"
    spec["automountServiceAccountToken"] = False
    spec["terminationGracePeriodSeconds"] = 30
    spec.pop("nodeName", None)
    spec.pop("schedulingGates", None)
    spec["nodeSelector"] = {"kubernetes.io/hostname": args.node}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": args.name,
            "namespace": source["metadata"]["namespace"],
            "labels": {
                "fs2.nebius.ai/test-only": "true",
                "snapshot.fs2.nebius/task": TASK,
                "snapshot.fs2.nebius/model": args.model_id,
            },
        },
        "spec": spec,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("donor", "restore"))
    for name in (
        "name",
        "node",
        "model-id",
        "model-revision",
        "container",
        "tools-image",
        "source-configmap",
        "pvc",
        "run",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--entrypoint-json", default="[]")
    parser.add_argument("--python", default="python3")
    parser.add_argument("--fallback", choices=("normal-load", "fail"), default="fail")
    parser.add_argument("--allow-device-remap", action="store_true")
    parser.add_argument(
        "--asyncio-loop",
        action="store_true",
        help="qualified CPU loop variant avoids libuv io_uring unsupported by CRIU",
    )
    parser.add_argument("--request-uid", type=int)
    args = parser.parse_args()
    print(json.dumps(render(json.loads(args.source.read_bytes()), args), indent=2))


if __name__ == "__main__":
    main()
