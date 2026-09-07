"""Optional, admission-frozen GPU restore over the normal scientific stage.

The bundle registry is operator configuration, not public request input. The
selected full record is frozen with each stage, so policy edits cannot change
an accepted run. The ordinary argv and artifact collector remain unchanged.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..snapshot_metadata import (
    SnapshotWorkerLog,
    prepare_worker_log_shadow,
    preserve_shared_snapshot_metadata,
    snapshot_cache_copy_command,
)

BUNDLE_SCHEMA = "fs2-serve.nebius.ai/scientific-snapshot-bundle/v1"
CAPTURED_TMP_PATH = "/tmp"  # noqa: S108 - per-Pod emptyDir at the captured runtime path


@dataclass(frozen=True, slots=True)
class _StageSnapshotAdapter:
    cli: tuple[str, str, str, str]
    sources: frozenset[str]
    entrypoint_key: str | None = None
    pythonpath: str | None = None


_ESMFOLD_ADAPTER = _StageSnapshotAdapter(
    ("/opt/esm/.pixi/envs/gpu/bin/python", "/opt/fs2/run_esmfold2.py", "run_esmfold2.py", "FS2_ESMFOLD2_WORKER_URL"),
    frozenset({"supervisor.py", "process_checkpoint.py", "esmfold2_server.py", "run_esmfold2.py"}),
)
# Adapter support does not qualify an image: each independently captured model
# still needs a matching immutable registry record and successful receipt.
_STAGE_SNAPSHOT_ADAPTERS = {
    ("protenix-v2", "sample-structure"): _StageSnapshotAdapter(
        ("/opt/protenix-venv/bin/python", "/opt/protenix-venv/bin/protenix", "protenix", "FS2_PROTENIX_WORKER_URL"),
        frozenset({"supervisor.py", "process_checkpoint.py", "protenix_server.py", "scientific_server.py"}),
    ),
    ("esmfold2", "fold"): _ESMFOLD_ADAPTER,
    ("esmfold2-fast", "fold"): _ESMFOLD_ADAPTER,
    ("rfdiffusion", "inference"): _StageSnapshotAdapter(
        ("/opt/conda/bin/python", "/opt/fs2/runtime_entrypoint.py", "rfdiffusion_observed_cli.py",
         "FS2_RFDIFFUSION_WORKER_URL"),
        frozenset({
            "supervisor.py", "process_checkpoint.py", "scientific_server.py", "rfdiffusion_server.py",
            "rfdiffusion_model_cache.py", "rfdiffusion_cli_proxy.py", "rfdiffusion_runtime_entrypoint.py",
            "sitecustomize.py",
        }),
        entrypoint_key="scientific_request_entrypoint.py",
        pythonpath="/snapshot-source:/opt/rfdiffusion",
    ),
}


@dataclass(frozen=True, slots=True)
class StageStartupPolicy:
    backend: str = "normal-load"
    bundle_id: str | None = None
    bundle_json: str | None = None

    def __post_init__(self) -> None:
        if self.backend == "normal-load":
            if self.bundle_id is not None or self.bundle_json is not None:
                raise ValueError("normal-load startup cannot select a snapshot bundle")
        elif self.backend == "cuda-criu":
            if not self.bundle_id or self.bundle_json is None:
                raise ValueError("cuda-criu startup requires a frozen snapshot bundle")
            bundle = validate_bundle(json.loads(self.bundle_json), self.bundle_id)
            if not bundle["qualified"]:
                raise ValueError("snapshot startup requires a qualified bundle")
        else:
            raise ValueError("scientific startup backend is unsupported")

    def to_value(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "bundle_id": self.bundle_id,
            "bundle": None if self.bundle_json is None else json.loads(self.bundle_json),
        }

    @classmethod
    def from_value(cls, value: Any) -> StageStartupPolicy:
        if not isinstance(value, dict) or set(value) != {"backend", "bundle_id", "bundle"}:
            raise ValueError("stored scientific startup policy fields differ")
        return cls(
            value["backend"], value["bundle_id"], None if value["bundle"] is None else canonical_bundle(value["bundle"])
        )


def canonical_bundle(bundle: Mapping[str, Any]) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_bundle(value: Any, bundle_id: str) -> dict[str, Any]:
    fields = {
        "schema",
        "bundle_id",
        "model_id",
        "stage_id",
        "model_revision",
        "profile_model_revision",
        "runtime_image",
        "tools_image",
        "source_configmap",
        "source_sha256",
        "cli_configmap",
        "cli_sha256",
        "pvc",
        "bundle_path",
        "manifest_sha256",
        "qualification_receipt_sha256",
        "qualified",
        "python",
        "cli_path",
        "cli_key",
        "worker_variable",
        "compatibility",
    }
    if not isinstance(value, Mapping) or set(value) - {"worker_log", "entrypoint"} != fields:
        raise ValueError("scientific snapshot bundle fields differ")
    bundle = copy.deepcopy(dict(value))
    if "worker_log" in bundle:
        SnapshotWorkerLog.model_validate(bundle["worker_log"])
    compatibility = bundle["compatibility"]
    if (
        not isinstance(compatibility, dict)
        or set(compatibility) != {"gpu_name", "compute_capability", "driver_version", "kernel_release"}
        or any(not isinstance(item, str) or not item for item in compatibility.values())
    ):
        raise ValueError("snapshot bundle must name its captured driver/GPU/kernel compatibility")
    if bundle["schema"] != BUNDLE_SCHEMA or bundle["bundle_id"] != bundle_id:
        raise ValueError("scientific snapshot bundle identity differs")
    adapter = _STAGE_SNAPSHOT_ADAPTERS.get((bundle["model_id"], bundle["stage_id"]))
    if adapter is None:
        raise ValueError("snapshot bundle has no qualified scientific stage adapter")
    entrypoint = bundle.get("entrypoint")
    if adapter.entrypoint_key is None:
        if "entrypoint" in bundle:
            raise ValueError("snapshot adapter retains its captured supervisor entrypoint")
    elif (
        not isinstance(entrypoint, dict)
        or set(entrypoint) != {"configmap", "key", "sha256"}
        or entrypoint["key"] != adapter.entrypoint_key
        or not isinstance(entrypoint["configmap"], str)
        or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", entrypoint["configmap"]) is None
        or not isinstance(entrypoint["sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", entrypoint["sha256"]) is None
    ):
        raise ValueError("snapshot adapter requires its exact versioned request entrypoint")
    for field in ("runtime_image", "tools_image"):
        if not isinstance(bundle[field], str) or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", bundle[field]) is None:
            raise ValueError("snapshot bundle images must be immutable")
    if not isinstance(bundle["qualified"], bool):
        raise ValueError("snapshot bundle qualification must be explicit")
    for field in ("manifest_sha256", "cli_sha256", "qualification_receipt_sha256"):
        if field == "qualification_receipt_sha256" and not bundle["qualified"] and bundle[field] is None:
            continue
        if not isinstance(bundle[field], str) or re.fullmatch(r"[a-f0-9]{64}", bundle[field]) is None:
            raise ValueError("snapshot bundle requires exact manifest/source/qualification digests")
    sources = bundle["source_sha256"]
    if (
        not isinstance(sources, dict)
        or set(sources) != adapter.sources
        or any(
            not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None for digest in sources.values()
        )
    ):
        raise ValueError("snapshot bundle requires the captured source identities")
    for field in ("source_configmap", "cli_configmap", "pvc"):
        if not isinstance(bundle[field], str) or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", bundle[field]) is None:
            raise ValueError("snapshot bundle Kubernetes source identity is invalid")
    if not isinstance(bundle["cli_key"], str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", bundle["cli_key"]) is None:
        raise ValueError("snapshot bundle ConfigMap key is invalid")
    path = PurePosixPath(bundle["bundle_path"])
    if path.is_absolute() or path.as_posix() != bundle["bundle_path"] or any(p in {".", ".."} for p in path.parts):
        raise ValueError("snapshot bundle path must be a contained relative path")
    if (bundle["python"], bundle["cli_path"], bundle["cli_key"], bundle["worker_variable"]) != adapter.cli:
        raise ValueError("snapshot bundle must preserve its qualified scientific CLI bridge")
    return bundle


def select_startup_policy(
    selection: Mapping[str, Any] | None,
    bundles: Mapping[str, Mapping[str, Any]],
    *,
    model_id: str,
    stage_id: str,
    model_revision: str,
    runtime_image: str,
) -> StageStartupPolicy:
    if selection is None:
        return StageStartupPolicy()
    if not isinstance(selection, Mapping) or set(selection) != {"backend", "bundle_id"}:
        raise ValueError("scientific startup selection fields differ")
    if selection["backend"] == "normal-load":
        return StageStartupPolicy("normal-load", selection["bundle_id"])
    bundle_id = selection["bundle_id"]
    if selection["backend"] != "cuda-criu" or not isinstance(bundle_id, str) or bundle_id not in bundles:
        raise ValueError("scientific startup selection names no registered snapshot bundle")
    bundle = validate_bundle(bundles[bundle_id], bundle_id)
    if (bundle["model_id"], bundle["stage_id"], bundle["profile_model_revision"], bundle["runtime_image"]) != (
        model_id,
        stage_id,
        model_revision,
        runtime_image,
    ):
        raise ValueError("snapshot bundle differs from the selected model/stage/runtime identity")
    return StageStartupPolicy("cuda-criu", bundle_id, canonical_bundle(bundle))


def apply_startup_policy(pod: dict[str, Any], policy: StageStartupPolicy, *, request_uid: int) -> dict[str, Any]:
    """Production form of the measured scientific restore transform.

    No probe labels, node pinning, scheduler bypass or resource changes are added.
    The supervisor retains its tested ordinary-load fallback and drops identity
    for the unchanged original scientific command.
    """
    if policy.backend == "normal-load":
        return pod
    assert policy.bundle_json is not None
    config = json.loads(policy.bundle_json)
    adapter = _STAGE_SNAPSHOT_ADAPTERS[(config["model_id"], config["stage_id"])]
    if request_uid != 10001:
        raise ValueError("snapshot bundle requires the captured scientific workspace identity")
    result = copy.deepcopy(pod)
    spec = result["spec"]
    preserve_shared_snapshot_metadata(spec)
    runtime = next(item for item in spec["containers"] if item["name"] == "scientific-stage")
    if runtime["image"] != config["runtime_image"]:
        raise ValueError("frozen snapshot runtime image differs from the rendered stage")
    original_command = runtime["command"] + runtime.pop("args", [])
    directory = "/checkpoints/" + config["bundle_path"]
    runtime["command"] = [
        config["python"],
        "/snapshot-entrypoint/" + config["entrypoint"]["key"]
        if adapter.entrypoint_key is not None else "/snapshot-source/supervisor.py",
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
    if adapter.entrypoint_key is not None:
        offset = runtime["command"].index("restore")
        runtime["command"][offset:offset] = [
            "--bundle-id", config["bundle_id"], "--manifest-sha256", config["manifest_sha256"],
        ]
    runtime["securityContext"] = {
        "runAsUser": 0,
        "runAsGroup": 0,
        "runAsNonRoot": False,
        "capabilities": {"add": ["SYS_ADMIN", "SYS_PTRACE", "CHECKPOINT_RESTORE", "NET_ADMIN", "SYS_TIME"]},
        "seccompProfile": {"type": "Unconfined"},
        "appArmorProfile": {"type": "Unconfined"},
    }
    runtime["env"].extend(
        [
            {"name": "FS2_SNAPSHOT_RUNTIME_IMAGE", "value": runtime["image"]},
            {"name": "FS2_SNAPSHOT_TOOLS_IMAGE", "value": config["tools_image"]},
            {"name": "FS2_MODEL_REVISION", "value": config["model_revision"]},
            {"name": "FS2_RUNTIME_ID", "value": config["model_id"]},
        ]
    )
    if adapter.pythonpath is not None:
        runtime["env"] = [item for item in runtime["env"] if item["name"] != "PYTHONPATH"]
        runtime["env"].append({"name": "PYTHONPATH", "value": adapter.pythonpath})
    if adapter.entrypoint_key is not None:
        runtime["volumeMounts"].append({
            "name": "snapshot-entrypoint", "mountPath": "/snapshot-entrypoint", "readOnly": True,
        })
        spec["volumes"].append({
            "name": "snapshot-entrypoint",
            "configMap": {"name": config["entrypoint"]["configmap"], "defaultMode": 292},
        })
    runtime["volumeMounts"].extend(
        [
            {"name": "snapshot-tools", "mountPath": "/tools"},
            {"name": "snapshot-source", "mountPath": "/snapshot-source", "readOnly": True},
            {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
        ]
    )
    if not any(mount["mountPath"] == CAPTURED_TMP_PATH for mount in runtime["volumeMounts"]):
        runtime["volumeMounts"].append({"name": "snapshot-checkpoints", "mountPath": CAPTURED_TMP_PATH})
    for mount in runtime["volumeMounts"]:
        if mount["mountPath"] in ("/runtime-cache", "/cache", CAPTURED_TMP_PATH):
            mount["name"] = "snapshot-checkpoints"
            mount["subPath"] = (
                config["bundle_path"] + "/" + ("tmp" if mount["mountPath"] == CAPTURED_TMP_PATH else "runtime-cache")
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
            {"name": "snapshot-cli", "mountPath": config["cli_path"], "subPath": config["cli_key"], "readOnly": True},
        ]
    )
    spec["volumes"].extend(
        [
            {"name": "snapshot-tools", "emptyDir": {}},
            {"name": "snapshot-source", "configMap": {"name": config["source_configmap"], "defaultMode": 292}},
            {"name": "snapshot-checkpoints", "emptyDir": {}},
            {"name": "snapshot-bundle", "persistentVolumeClaim": {"claimName": config["pvc"], "readOnly": True}},
            {"name": "snapshot-cli", "configMap": {"name": config["cli_configmap"], "defaultMode": 365}},
        ]
    )
    spec["initContainers"].insert(
        0,
        {
            "name": "snapshot-tools",
            "image": config["tools_image"],
            "command": [
                "/bin/sh",
                "-c",
                (
                    "cp -a /snapshot-binaries/. /tools/ && "
                    "cp -L /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 "
                    "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/ && "
                    'mkdir -p "$1/runtime-cache" "$1/tmp" && '
                    'if [ -n "$2" ]; then chown "$2:$2" "$1/runtime-cache" "$1/tmp"; fi'
                    ' && ' + snapshot_cache_copy_command()
                ),
                "snapshot-tools",
                directory,
                "10001",
            ],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
            "volumeMounts": [
                {"name": "snapshot-tools", "mountPath": "/tools"},
                {"name": "snapshot-checkpoints", "mountPath": "/checkpoints"},
                bundle_mount,
            ],
        },
    )
    prepare_worker_log_shadow(
        spec["initContainers"][0], runtime, directory,
        SnapshotWorkerLog.model_validate(config["worker_log"]) if config.get("worker_log") else None,
    )
    result.setdefault("metadata", {}).setdefault("annotations", {}).update(
        {
            "fs2.nebius.ai/startup-backend": policy.backend,
            "fs2.nebius.ai/snapshot-bundle-id": policy.bundle_id,
            "fs2.nebius.ai/snapshot-manifest-sha256": config["manifest_sha256"],
        }
    )
    return result
