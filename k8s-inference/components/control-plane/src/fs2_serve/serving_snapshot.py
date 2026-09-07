"""Qualified, optional CUDA+CRIU startup for existing serving templates.

The registry is operator configuration, not a request-supplied Pod template.
Scheduling, replicas, original model arguments and resource bounds remain owned
by ModelDeployment. Each bundle is specific to its measured runtime and GPU
class; the worker additionally checks the real driver/kernel before restoring.
"""

from __future__ import annotations

import copy
import re
from ipaddress import IPv4Address
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import StrictModel
from .snapshot_metadata import (
    SnapshotWorkerLog,
    prepare_worker_log_shadow,
    preserve_shared_snapshot_metadata,
    snapshot_cache_copy_command,
)

CAPTURED_TMP_PATH = "/tmp"  # noqa: S108 - captured per-Pod emptyDir mount, never host tmp
DEFAULT_SUPERVISOR_PATH = "/tools/usr/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class ServingSnapshotBundle(StrictModel):
    schema_version: Literal["fs2-serve.nebius.ai/serving-snapshot-bundle/v1"] = Field(alias="schema")
    bundle_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    model_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    runtime_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    model_revision: str = Field(min_length=1, max_length=256)
    artifact_manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    qualification_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    qualified: Literal[True]
    accelerator_classes: list[str] = Field(min_length=1, max_length=16)
    compatibility: dict[str, str]
    tools_image: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$")
    source_configmap: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    source_sha256: dict[str, str]
    entrypoint_configmap: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    entrypoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    network_configmap: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    address_configmap: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    pvc: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")
    bundle_path: str = Field(min_length=1, max_length=128)
    captured_pod_ip: str
    image_entrypoint: list[str] = Field(min_length=1, max_length=16)
    runtime_command: list[str] = Field(min_length=1, max_length=128)
    supervisor_python: str = Field(default="python3", min_length=1, max_length=1024)
    supervisor_path: str = Field(default=DEFAULT_SUPERVISOR_PATH, min_length=1, max_length=4096)
    address_python: str = Field(default="python3", min_length=1, max_length=1024)
    fallback_command_prefix: list[str] = Field(
        default_factory=lambda: ["python3", "/snapshot-source/serving_launcher.py"],
        min_length=2,
        max_length=16,
    )
    worker_log: SnapshotWorkerLog | None = None

    @model_validator(mode="after")
    def exact_captured_dependencies(self) -> ServingSnapshotBundle:
        expected_sources = {
            "process_checkpoint.py",
            "serving_checkpoint.py",
            "serving_filesystem.py",
            "serving_launcher.py",
            "serving_supervisor.py",
            "sitecustomize.py",
            "supervisor.py",
        }
        source_names = set(self.source_sha256)
        working_directory_source = "working_directory_launcher.py"
        allowed_source_sets = (
            expected_sources,
            expected_sources | {working_directory_source},
        )
        if source_names not in allowed_source_sets:
            raise ValueError("serving snapshot requires the captured source set")
        if any(re.fullmatch(r"[a-f0-9]{64}", digest) is None for digest in self.source_sha256.values()):
            raise ValueError("serving snapshot source digests differ")
        if set(self.compatibility) != {"gpu_name", "compute_capability", "driver_version", "kernel_release"}:
            raise ValueError("serving snapshot must state GPU/driver/kernel compatibility")
        if any(not value for value in self.compatibility.values()):
            raise ValueError("serving snapshot compatibility cannot be empty")
        default_prefix = ["python3", "/snapshot-source/serving_launcher.py"]
        prefix = self.fallback_command_prefix
        uses_working_directory_launcher = (
            len(prefix) == 9
            and prefix[0:2]
            == ["python3", "/snapshot-source/working_directory_launcher.py"]
            and prefix[2] == "--directory"
            and PurePosixPath(prefix[3]).is_absolute()
            and ".." not in PurePosixPath(prefix[3]).parts
            and prefix[4] == "--uid"
            and prefix[5].isdigit()
            and int(prefix[5]) > 0
            and prefix[6] == "--gid"
            and prefix[7].isdigit()
            and int(prefix[7]) > 0
            and prefix[8] == "--"
        )
        if prefix != default_prefix and not uses_working_directory_launcher:
            raise ValueError("serving snapshot fallback launcher differs")
        if (working_directory_source in source_names) != uses_working_directory_launcher:
            raise ValueError("serving snapshot working-directory source differs from fallback launcher")
        path = PurePosixPath(self.bundle_path)
        if path.is_absolute() or path.as_posix() != self.bundle_path or any(part in {".", ".."} for part in path.parts):
            raise ValueError("serving snapshot requires a contained bundle path")
        IPv4Address(self.captured_pod_ip)
        return self


def configure_serving_snapshot(
    pod_spec: dict[str, Any],
    *,
    config: ServingSnapshotBundle,
    runtime_container_name: str,
    fallback: Literal["normal-load", "fail"],
) -> None:
    """Apply the exact tested bridge without replacing the workload template."""
    if pod_spec.get("hostNetwork"):
        raise ValueError("serving restore requires a private Pod network namespace")
    preserve_shared_snapshot_metadata(pod_spec)
    runtime = next(item for item in pod_spec["containers"] if item["name"] == runtime_container_name)
    original = (runtime.get("command") or config.image_entrypoint) + runtime.get("args", [])
    if runtime["image"] != config.runtime_image or original != config.runtime_command:
        raise ValueError("serving snapshot differs from the current runtime image or original arguments")
    directory = "/checkpoints/" + config.bundle_path
    runtime["command"] = [
        config.supervisor_python,
        "/snapshot-entrypoint/serving_entrypoint.py",
        "--directory",
        directory,
        "--source-directory",
        "/snapshot-bundle",
        "--request-mode",
        "server",
        "--fallback",
        fallback,
        "--allow-device-remap",
        "restore",
        "--",
        *config.fallback_command_prefix,
        *original,
    ]
    runtime.pop("args", None)
    runtime["securityContext"] = {
        "runAsUser": 0,
        "runAsGroup": 0,
        "runAsNonRoot": False,
        "capabilities": {"add": ["SYS_ADMIN", "SYS_PTRACE", "CHECKPOINT_RESTORE", "NET_ADMIN", "SYS_TIME"]},
        "seccompProfile": {"type": "Unconfined"},
        "appArmorProfile": {"type": "Unconfined"},
    }
    values = {
        "FS2_SNAPSHOT_RUNTIME_IMAGE": runtime["image"],
        "FS2_SNAPSHOT_TOOLS_IMAGE": config.tools_image,
        "FS2_MODEL_REVISION": config.model_revision,
        "FS2_RUNTIME_ID": config.model_ref,
        "FS2_SNAPSHOT_READY_FILE": directory + "/runtime-ready.json",
        "USE_LIBUV": "0",
        "FLASHINFER_WORKSPACE_BASE": directory + "/cache/flashinfer-workspace",
        "PATH": config.supervisor_path,
    }
    runtime["env"] = [item for item in runtime.get("env", []) if item["name"] not in values]
    runtime["env"].extend({"name": key, "value": value} for key, value in values.items())
    mounts = runtime.setdefault("volumeMounts", [])
    if not any(item["mountPath"] == CAPTURED_TMP_PATH for item in mounts):
        mounts.append({"name": "snapshot-checkpoints", "mountPath": CAPTURED_TMP_PATH})
    for mount in mounts:
        if mount["mountPath"] in {"/runtime-cache", "/cache", CAPTURED_TMP_PATH}:
            mount["name"] = "snapshot-checkpoints"
            mount["subPath"] = (
                config.bundle_path + "/" + ("tmp" if mount["mountPath"] == CAPTURED_TMP_PATH else "runtime-cache")
            )
    bundle_mount = {
        "name": "snapshot-bundle",
        "mountPath": "/snapshot-bundle",
        "subPath": config.bundle_path,
        "readOnly": True,
    }
    mounts.extend(
        [
            {"name": "snapshot-tools", "mountPath": "/tools"},
            {"name": "snapshot-source", "mountPath": "/snapshot-source", "readOnly": True},
            {"name": "snapshot-entrypoint", "mountPath": "/snapshot-entrypoint", "readOnly": True},
            {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
            bundle_mount,
            {
                "name": "snapshot-bundle",
                "mountPath": directory + "/images",
                "subPath": config.bundle_path + "/images",
                "readOnly": True,
            },
        ]
    )
    pod_spec.setdefault("volumes", []).extend(
        [
            {"name": "snapshot-tools", "emptyDir": {}},
            {"name": "snapshot-checkpoints", "emptyDir": {}},
            {"name": "snapshot-source", "configMap": {"name": config.source_configmap, "defaultMode": 292}},
            {"name": "snapshot-entrypoint", "configMap": {"name": config.entrypoint_configmap, "defaultMode": 292}},
            {"name": "snapshot-network-source", "configMap": {"name": config.network_configmap, "defaultMode": 365}},
            {"name": "snapshot-address-source", "configMap": {"name": config.address_configmap, "defaultMode": 292}},
            {"name": "snapshot-bundle", "persistentVolumeClaim": {"claimName": config.pvc, "readOnly": True}},
        ]
    )
    initializer = {
        "name": "snapshot-tools",
        "image": config.tools_image,
        "command": [
            "/bin/sh",
            "-c",
            (
                "cp -a /snapshot-binaries/. /tools/ && "
                "cp -L /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 "
                "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/ && "
                'mkdir -p "$1/runtime-cache" "$1/tmp" && '
                + snapshot_cache_copy_command() + ' && '
                "mkdir -p /tools/usr/sbin /tools/usr/lib/x86_64-linux-gnu && "
                "cp -L /usr/sbin/xtables-nft-multi /tools/usr/sbin/ && "
                "cp -L /usr/lib/x86_64-linux-gnu/libxtables.so.12 /usr/lib/x86_64-linux-gnu/libmnl.so.0 "
                "/usr/lib/x86_64-linux-gnu/libnftnl.so.11 /tools/usr/lib/x86_64-linux-gnu/ && "
                "cp -a /usr/lib/x86_64-linux-gnu/xtables /tools/usr/lib/x86_64-linux-gnu/ && "
                "for tool in iptables ip6tables; do cp /snapshot-network-source/iptables /tools/usr/sbin/$tool; "
                "chmod 0555 /tools/usr/sbin/$tool; done"
            ),
            "snapshot-tools",
            directory,
        ],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
        "volumeMounts": [
            {"name": "snapshot-tools", "mountPath": "/tools"},
            {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
            copy.deepcopy(bundle_mount),
            {"name": "snapshot-network-source", "mountPath": "/snapshot-network-source", "readOnly": True},
        ],
    }
    address_initializer = {
        "name": "snapshot-local-address",
        "image": runtime["image"],
        "command": [config.address_python, "/snapshot-address/restore_loopback_address.py", config.captured_pod_ip],
        "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
        "securityContext": {
            "runAsUser": 0,
            "runAsGroup": 0,
            "runAsNonRoot": False,
            "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
        },
        "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "128Mi"}},
        "volumeMounts": [{"name": "snapshot-address-source", "mountPath": "/snapshot-address", "readOnly": True}],
    }
    prepare_worker_log_shadow(initializer, runtime, directory, config.worker_log)
    pod_spec.setdefault("initContainers", [])[:0] = [address_initializer, initializer]
    readiness_probe = runtime.get("readinessProbe")
    if not isinstance(readiness_probe, dict):
        raise ValueError("serving snapshot requires the original readiness probe")
    if "startupProbe" not in runtime:
        # Some qualified serving images use a long-failure-threshold readiness
        # probe instead of a distinct startup probe. Preserve that exact gate
        # for the snapshot supervisor rather than hiding an otherwise valid
        # bundle from the admin configuration options.
        runtime["startupProbe"] = copy.deepcopy(readiness_probe)
    for name in ("startupProbe", "readinessProbe"):
        probe = runtime.get(name)
        if not isinstance(probe, dict):
            raise ValueError("serving snapshot requires the original startup and readiness probes")
        for handler in ("httpGet", "tcpSocket", "grpc", "exec"):
            probe.pop(handler, None)
        probe["exec"] = {"command": [config.supervisor_python, "/snapshot-entrypoint/serving_entrypoint.py", "ready"]}
