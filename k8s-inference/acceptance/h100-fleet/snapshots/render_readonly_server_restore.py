#!/usr/bin/env python3
"""Restore an exact captured server Pod with immutable bundle/private scratch."""

import argparse
import copy
import json
from pathlib import Path


def install_network_tools(spec, runtime, configmap):
    """Use the pinned nft-backed helper inside this Pod's network namespace."""
    initializer = next(
        item for item in spec["initContainers"] if item["name"] == "snapshot-tools"
    )
    spec["volumes"].append(
        {
            "name": "snapshot-network-source",
            "configMap": {"name": configmap, "defaultMode": 365},
        }
    )
    initializer["volumeMounts"].append(
        {
            "name": "snapshot-network-source",
            "mountPath": "/snapshot-network-source",
            "readOnly": True,
        }
    )
    initializer["command"][2] += (
        " && mkdir -p /tools/usr/sbin /tools/usr/lib/x86_64-linux-gnu"
        " && cp -L /usr/sbin/xtables-nft-multi /tools/usr/sbin/"
        " && cp -L /usr/lib/x86_64-linux-gnu/libxtables.so.12 /usr/lib/x86_64-linux-gnu/libmnl.so.0"
        " /usr/lib/x86_64-linux-gnu/libnftnl.so.11 /tools/usr/lib/x86_64-linux-gnu/"
        " && cp -a /usr/lib/x86_64-linux-gnu/xtables /tools/usr/lib/x86_64-linux-gnu/"
        " && for tool in iptables ip6tables; do cp /snapshot-network-source/iptables /tools/usr/sbin/$tool;"
        " chmod 0555 /tools/usr/sbin/$tool; done"
    )
    runtime.setdefault("env", []).append(
        {
            "name": "PATH",
            "value": "/tools/usr/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
    )


def render(
    source, *, name, container, pvc=None, network_configmap=None, address_configmap=None
):
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": source["metadata"]["namespace"],
            "labels": copy.deepcopy(source["metadata"]["labels"]),
        },
        "spec": copy.deepcopy(source["spec"]),
    }
    spec = pod["spec"]
    spec.pop("nodeName", None)
    runtime = next(item for item in spec["containers"] if item["name"] == container)
    command = runtime["command"]
    directory = command[command.index("--directory") + 1]
    relative = directory.removeprefix("/checkpoints/")
    offset = command.index("donor")
    command[offset : offset + 1] = [
        "--source-directory",
        "/snapshot-bundle",
        "--allow-device-remap",
        "restore",
    ]
    capabilities = runtime["securityContext"]["capabilities"]["add"]
    if "SYS_TIME" not in capabilities:
        capabilities.append("SYS_TIME")
    volume = next(
        item for item in spec["volumes"] if item["name"] == "snapshot-checkpoints"
    )
    claim = pvc or volume["persistentVolumeClaim"]["claimName"]
    volume.clear()
    volume.update(name="snapshot-checkpoints", emptyDir={})
    spec["volumes"].append(
        {
            "name": "snapshot-bundle",
            "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
        }
    )
    mount = {
        "name": "snapshot-bundle",
        "mountPath": "/snapshot-bundle",
        "subPath": relative,
        "readOnly": True,
    }
    runtime["volumeMounts"].extend(
        [
            mount,
            {
                "name": "snapshot-bundle",
                "mountPath": directory + "/images",
                "subPath": relative + "/images",
                "readOnly": True,
            },
        ]
    )
    initializer = next(
        item for item in spec["initContainers"] if item["name"] == "snapshot-tools"
    )
    initializer["volumeMounts"].append(mount)
    from fs2_serve.snapshot_metadata import snapshot_cache_copy_command

    initializer["command"][2] = initializer["command"][2].replace('"$1/cache" ', "") + " && " + snapshot_cache_copy_command()
    if network_configmap and not any(
        volume["name"] == "snapshot-network-source" for volume in spec["volumes"]
    ):
        install_network_tools(spec, runtime, network_configmap)
    if address_configmap:
        if spec.get("hostNetwork"):
            raise ValueError(
                "captured address restoration requires a private Pod network namespace"
            )
        address = source["status"]["podIP"]
        spec["volumes"].append(
            {
                "name": "snapshot-address-source",
                "configMap": {"name": address_configmap, "defaultMode": 292},
            }
        )
        spec["initContainers"].insert(
            0,
            {
                "name": "snapshot-local-address",
                "image": runtime["image"],
                "command": [
                    "python3",
                    "/snapshot-address/restore_loopback_address.py",
                    address,
                ],
                "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                "securityContext": {
                    "runAsUser": 0,
                    "runAsGroup": 0,
                    "runAsNonRoot": False,
                    "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "128Mi"},
                },
                "volumeMounts": [
                    {
                        "name": "snapshot-address-source",
                        "mountPath": "/snapshot-address",
                        "readOnly": True,
                    }
                ],
            },
        )
    return pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--pvc")
    parser.add_argument("--network-configmap")
    parser.add_argument("--address-configmap")
    args = parser.parse_args()
    print(
        json.dumps(
            render(
                json.loads(args.source.read_bytes()),
                name=args.name,
                container=args.container,
                pvc=args.pvc,
                network_configmap=args.network_configmap,
                address_configmap=args.address_configmap,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
