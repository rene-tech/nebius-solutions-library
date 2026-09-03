#!/usr/bin/env python3
"""Render immutable, CPU-only Kubernetes Jobs for public artifact staging."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from public_artifacts import ContractError, canonical_json, load_json, sha256_bytes, validate_catalog


DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
DIGEST_IMAGE = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
COMMIT = re.compile(r"^[a-f0-9]{8,64}$")
FILESYSTEM_ID = re.compile(r"^computefilesystem-[a-z0-9]+$")
DEFAULT_IMAGE = "docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"


def _dns(value: str, context: str) -> str:
    if not DNS_LABEL.fullmatch(value):
        raise ContractError(f"{context} must be a Kubernetes DNS label")
    return value


def _dedicated_cpu_selector(raw: str) -> dict[str, str]:
    selector = json.loads(raw)
    if not isinstance(selector, dict) or not selector or not all(
        isinstance(key, str) and isinstance(value, str) and key and value
        for key, value in selector.items()
    ):
        raise ContractError("node selector must be a non-empty JSON object of strings")
    if selector.get("workload.fs2.nebius/reference-data") != "true":
        raise ContractError("node selector must require the dedicated reference-data workload label")
    if selector.get("capacity.fs2.nebius/type") != "regular":
        raise ContractError("artifact downloads require a regular CPU pool")
    pool = selector.get("capacity.fs2.nebius/pool")
    if pool != "reference-data":
        raise ContractError("artifact downloads require the reference-data pool; the shared system pool is forbidden")
    if selector.get("storage.fs2.nebius/reference-data") != "true":
        raise ContractError("dedicated CPU pool must have the reference-data filesystem attached")
    return selector


def _dedicated_cpu_toleration(raw: str) -> dict[str, str]:
    toleration = json.loads(raw)
    expected = {
        "key": "workload.fs2.nebius/reference-data",
        "operator": "Equal",
        "value": "true",
        "effect": "NoSchedule",
    }
    if toleration != expected:
        raise ContractError("node toleration must exactly match the dedicated reference-data CPU taint")
    return toleration


def render(args: argparse.Namespace) -> dict[str, Any]:
    catalog = validate_catalog(load_json(args.catalog), args.catalog)
    if not DIGEST_IMAGE.fullmatch(args.image):
        raise ContractError("ingestion image must be digest pinned")
    if not COMMIT.fullmatch(args.source_commit) or not COMMIT.fullmatch(args.reference_plane_source_commit):
        raise ContractError("source commits must be immutable hexadecimal commits")
    if not FILESYSTEM_ID.fullmatch(args.filesystem_id):
        raise ContractError("filesystem ID must identify a managed filesystem")
    namespace = _dns(args.namespace, "namespace")
    if namespace != "fs2-reference-data":
        raise ContractError("ingestion requires the isolated fs2-reference-data namespace")
    local_queue = _dns(args.local_queue, "local queue")
    service_account = _dns(args.service_account, "service account")
    selector = _dedicated_cpu_selector(args.node_selector)
    toleration = _dedicated_cpu_toleration(args.node_toleration)
    cpu_pool_label = selector["capacity.fs2.nebius/pool"]
    if not args.cpu_pool_id or not args.cpu_pool_name:
        raise ContractError("the infrastructure-owned CPU pool ID and name are required")
    if args.filesystem_size_gib < 2048:
        raise ContractError("the integrated regional reference filesystem must be at least 2048 GiB")
    if not args.shared_filesystem_host_path.startswith("/mnt/") or ".." in args.shared_filesystem_host_path:
        raise ContractError("shared filesystem host path must be a safe absolute path below /mnt")
    cache_parts = Path(args.cache_subpath).parts
    if not cache_parts or args.cache_subpath.startswith("/") or ".." in cache_parts:
        raise ContractError("cache subpath must be a safe relative path")
    cache_root = f"/reference-data/{args.cache_subpath.rstrip('/')}"
    available = {
        artifact_id: entry
        for artifact_id, entry in catalog["artifacts"].items()
        if entry["state"] == "available"
    }
    selected = sorted(available) if not args.artifact else sorted(set(args.artifact))
    unknown = set(selected) - set(available)
    if unknown:
        raise ContractError(f"selected artifacts are not available: {sorted(unknown)}")
    catalog_digest = sha256_bytes(canonical_json(load_json(args.catalog)))
    config_name = _dns(f"public-artifacts-{catalog_digest[:12]}", "ConfigMap name")
    data = {
        "artifact-catalog.json": args.catalog.read_text(encoding="utf-8"),
        "public_artifacts.py": Path(__file__).with_name("public_artifacts.py").read_text(encoding="utf-8"),
    }
    for entry in available.values():
        manifest_path = Path(entry["_manifest_path"])
        relative = manifest_path.relative_to(args.catalog.parent.resolve()).as_posix()
        data[relative.replace("/", "__")] = manifest_path.read_text(encoding="utf-8")
    resources: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/name": "public-artifact-ingestion",
                    "app.kubernetes.io/managed-by": "fs2-task-branch",
                    "fs2.nebius.ai/catalog-digest": catalog_digest[:63],
                },
            },
            "immutable": True,
            "data": data,
        }
    ]
    for artifact_id in selected:
        entry = available[artifact_id]
        manifest_digest = entry["_manifest_digest"]
        job_name = _dns(f"artifact-{artifact_id[:38].rstrip('-')}-{manifest_digest[:12]}", "Job name")
        command = [
            "python3", "/work/public_artifacts.py", "stage",
            "--catalog", "/work/artifact-catalog.json",
            "--artifact", artifact_id,
            "--cache-root", cache_root,
            "--project-id", args.project_id,
            "--region", args.region,
            "--cluster", args.cluster,
            "--filesystem-id", args.filesystem_id,
            "--filesystem-size-gib", str(args.filesystem_size_gib),
            "--namespace", namespace,
            "--local-queue", local_queue,
            "--cpu-pool-id", args.cpu_pool_id,
            "--cpu-pool-name", args.cpu_pool_name,
            "--cpu-pool-label", cpu_pool_label,
            "--shared-filesystem-host-path", args.shared_filesystem_host_path,
            "--cache-subpath", args.cache_subpath,
            "--reference-plane-source-commit", args.reference_plane_source_commit,
            "--source-commit", args.source_commit,
        ]
        resources.append(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": job_name,
                    "namespace": namespace,
                    "labels": {
                        "app.kubernetes.io/name": "public-artifact-ingestion",
                        "fs2.nebius.ai/artifact-id": artifact_id,
                        "fs2.nebius.ai/manifest-digest": manifest_digest[:63],
                        "kueue.x-k8s.io/queue-name": local_queue,
                        "kueue.x-k8s.io/priority-class": "batch",
                    },
                    "annotations": {
                        "fs2.nebius.ai/source-commit": args.source_commit,
                        "fs2.nebius.ai/catalog-digest": catalog_digest,
                        "fs2.nebius.ai/reference-plane-source-commit": args.reference_plane_source_commit,
                        "fs2.nebius.ai/filesystem-id": args.filesystem_id,
                        "fs2.nebius.ai/cpu-pool-id": args.cpu_pool_id,
                        "fs2.nebius.ai/cpu-pool-name": args.cpu_pool_name,
                    },
                },
                "spec": {
                    "suspend": True,
                    "backoffLimit": 3,
                    "activeDeadlineSeconds": args.active_deadline_seconds,
                    "ttlSecondsAfterFinished": args.ttl_seconds,
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": "public-artifact-ingestion",
                                "fs2.nebius.ai/artifact-id": artifact_id,
                                "reference-data.fs2.nebius.ai/network-mode": "public-source-staging",
                            }
                        },
                        "spec": {
                            "restartPolicy": "Never",
                            "serviceAccountName": service_account,
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "terminationGracePeriodSeconds": 30,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                                "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "nodeSelector": selector,
                            "tolerations": [toleration],
                            "containers": [
                                {
                                    "name": "ingest",
                                    "image": args.image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": command,
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "250m", "memory": "512Mi"},
                                        "limits": {"cpu": "2", "memory": "2Gi"},
                                    },
                                    "volumeMounts": [
                                        {"name": "program", "mountPath": "/work", "readOnly": True},
                                        {"name": "reference-data", "mountPath": "/reference-data"},
                                        {"name": "tmp", "mountPath": "/tmp"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "program", "configMap": {"name": config_name, "defaultMode": 292}},
                                {
                                    "name": "reference-data",
                                    "hostPath": {
                                        "path": args.shared_filesystem_host_path,
                                        "type": "Directory",
                                    },
                                },
                                {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                            ],
                        },
                    },
                },
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": resources}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--catalog", type=Path, required=True)
    result.add_argument("--artifact", action="append")
    result.add_argument("--namespace", required=True)
    result.add_argument("--local-queue", required=True)
    result.add_argument("--service-account", required=True)
    result.add_argument("--shared-filesystem-host-path", required=True)
    result.add_argument("--cache-subpath", default="model-artifacts/public/v1")
    result.add_argument("--image", default=DEFAULT_IMAGE)
    result.add_argument("--project-id", required=True)
    result.add_argument("--region", required=True)
    result.add_argument("--cluster", required=True)
    result.add_argument("--filesystem-id", required=True)
    result.add_argument("--filesystem-size-gib", type=int, required=True)
    result.add_argument("--cpu-pool-id", required=True)
    result.add_argument("--cpu-pool-name", required=True)
    result.add_argument("--reference-plane-source-commit", required=True)
    result.add_argument("--source-commit", required=True)
    result.add_argument("--node-selector", required=True)
    result.add_argument("--node-toleration", required=True)
    result.add_argument("--active-deadline-seconds", type=int, default=21600)
    result.add_argument("--ttl-seconds", type=int, default=86400)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        print(json.dumps(render(args), indent=2, sort_keys=True))
        return 0
    except (ContractError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
