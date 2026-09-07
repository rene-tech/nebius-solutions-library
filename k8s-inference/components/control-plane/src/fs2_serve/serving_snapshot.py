"""Qualified, optional CUDA+CRIU startup for existing serving templates.

The registry is operator configuration, not a request-supplied Pod template.
Scheduling, replicas, original model arguments and resource bounds remain owned
by ModelDeployment. Each bundle is specific to its measured runtime and GPU
class; the worker additionally checks the real driver/kernel before restoring.
"""

from __future__ import annotations

import copy
import re
import shlex
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
    snapshot_copy_command,
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


def _snapshot_probe_command(
    runtime: dict[str, Any], probe: dict[str, Any], config: ServingSnapshotBundle
) -> list[str]:
    """Keep the native HTTP gate in addition to the immutable restore marker."""
    default = [config.supervisor_python, "/snapshot-entrypoint/serving_entrypoint.py", "ready"]
    http = probe.get("httpGet")
    if not isinstance(http, dict):
        return default
    port = http["port"]
    if isinstance(port, str):
        port = next(
            (item["containerPort"] for item in runtime.get("ports", []) if item.get("name") == port),
            None,
        )
        if port is None:
            raise ValueError("serving snapshot HTTP probe names an unknown container port")
    host = http.get("host") or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = http.get("scheme", "HTTP").lower()
    path = http.get("path") or "/"
    url = f"{scheme}://{host}:{port}{path}"
    headers = {item["name"]: item["value"] for item in http.get("httpHeaders", [])}
    if url == "http://127.0.0.1:8000/health" and not headers:
        # Retain the exact already-qualified default Qwen/Cosmos/CXR command.
        return default
    code = (
        "import os, sys; from pathlib import Path; from urllib.request import Request; "
        "sys.path.insert(0, '/snapshot-entrypoint'); from serving_entrypoint import readiness; "
        "raise SystemExit(0 if readiness(Path(os.environ['FS2_SNAPSHOT_READY_FILE']), "
        f"Request({url!r}, headers={headers!r})) else 1)"
    )
    return [config.supervisor_python, "-c", code]


def _isolate_vllm_cache(
    pod_spec: dict[str, Any], runtime: dict[str, Any], initializer: dict[str, Any], directory: str
) -> None:
    """Keep captured absolute vLLM paths without root writes to the native PVC.

    The frozen supervisor relocates CUDA/Triton caches but not VLLM_CACHE_ROOT.
    vLLM's root also holds AOT artifacts, so seed the complete small subtree,
    preserving mapped-file bytes/metadata, rather than only the autotune JSON.
    The nested mount keeps the restored process's captured environment intact.
    """
    value = next((item.get("value") for item in runtime.get("env", []) if item["name"] == "VLLM_CACHE_ROOT"), None)
    if not value:
        return
    cache_path = PurePosixPath(value)
    mounts = runtime["volumeMounts"]
    candidates = [item for item in mounts if cache_path.is_relative_to(PurePosixPath(item["mountPath"]))]
    if not candidates:
        return  # Container-local caches do not contaminate a shared native PVC.
    source = max(candidates, key=lambda item: len(PurePosixPath(item["mountPath"]).parts))
    volume = next(item for item in pod_spec["volumes"] if item["name"] == source["name"])
    if "persistentVolumeClaim" not in volume:
        return  # Already bound to per-Pod snapshot scratch or another emptyDir.
    relative = cache_path.relative_to(PurePosixPath(source["mountPath"]))
    source_mount = copy.deepcopy(source)
    source_mount.update(mountPath="/snapshot-native-vllm-source", readOnly=True)
    initializer["volumeMounts"].append(source_mount)
    origin = str(PurePosixPath(source_mount["mountPath"]) / relative)
    destination = directory + "/native-vllm-cache"
    initializer["command"][2] += (
        f" && mkdir -p {shlex.quote(destination)} && if [ -d {shlex.quote(origin)} ]; then "
        + snapshot_copy_command(origin, destination)
        + "; fi"
    )
    shadow = {"name": "snapshot-checkpoints", "mountPath": value,
              "subPath": directory.removeprefix("/checkpoints/") + "/native-vllm-cache"}
    if source["mountPath"] == value:
        mounts[mounts.index(source)] = shadow
    else:
        mounts.append(shadow)


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
    _isolate_vllm_cache(pod_spec, runtime, initializer, directory)
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
        command = _snapshot_probe_command(runtime, probe, config)
        for handler in ("httpGet", "tcpSocket", "grpc", "exec"):
            probe.pop(handler, None)
        probe["exec"] = {"command": command}
