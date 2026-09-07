#!/usr/bin/env python3
"""Recapture an exact previous task donor with durable named shared memory."""

import argparse
import copy
import json
from pathlib import Path

from render_readonly_server_restore import install_network_tools


def render(
    source, *, name, container, run, source_configmap, network_configmap=None, pvc=None
):
    spec = copy.deepcopy(source["spec"])
    spec.pop("nodeName", None)
    spec.pop("ephemeralContainers", None)
    runtime = next(item for item in spec["containers"] if item["name"] == container)
    old = runtime["command"][runtime["command"].index("--directory") + 1].removeprefix(
        "/checkpoints/"
    )
    runtime["command"] = [
        value.replace(
            "/snapshot-source/supervisor.py", "/snapshot-source/serving_supervisor.py"
        ).replace("/checkpoints/" + old, "/checkpoints/" + run)
        for value in runtime["command"]
    ]
    for item in spec["containers"] + spec["initContainers"]:
        if item["name"] == "snapshot-tools":
            item["command"] = [
                value.replace("/checkpoints/" + old, "/checkpoints/" + run)
                for value in item["command"]
            ]
        for mount in item.get("volumeMounts", []):
            if mount.get("subPath", "").startswith(old + "/"):
                mount["subPath"] = run + mount["subPath"][len(old) :]
    next(item for item in spec["volumes"] if item["name"] == "snapshot-source")[
        "configMap"
    ]["name"] = source_configmap
    if pvc:
        next(
            item for item in spec["volumes"] if item["name"] == "snapshot-checkpoints"
        )["persistentVolumeClaim"]["claimName"] = pvc
    if network_configmap:
        install_network_tools(spec, runtime, network_configmap)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": source["metadata"]["namespace"],
            "labels": copy.deepcopy(source["metadata"]["labels"]),
        },
        "spec": spec,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("name", "container", "run", "source-configmap"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--network-configmap")
    parser.add_argument("--pvc")
    args = parser.parse_args()
    print(
        json.dumps(
            render(
                json.loads(args.source.read_bytes()),
                name=args.name,
                container=args.container,
                run=args.run,
                source_configmap=args.source_configmap,
                network_configmap=args.network_configmap,
                pvc=args.pvc,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
