#!/usr/bin/env python3
"""Render Kueue-compatible CPU-only staging and private preprocessing Jobs.

Output is Kubernetes JSON (accepted directly by kubectl). Customer sequence
content and credentials are never embedded; only immutable object references
and optional Secret key references are rendered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

from reference_data import (
    ContractError,
    canonical_json,
    load_json,
    validate_access_receipt,
    validate_catalog,
    validate_preprocess_request,
)


DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _dns_label(value: str, context: str) -> str:
    if len(value) > 63 or not DNS_LABEL_RE.fullmatch(value):
        raise ContractError(f"{context} must be a Kubernetes DNS label")
    return value


def _base_job(
    *,
    name: str,
    namespace: str,
    queue: str,
    image: str,
    command: list[str],
    labels: Mapping[str, str],
    cpu: str,
    memory: str,
    ephemeral_storage: str,
    active_deadline_seconds: int,
    backoff_limit: int,
    shared_host_path: str,
    reference_data_read_only: bool,
    telemetry_host_path: str | None,
    config_maps: list[dict[str, str]],
    credentials_secret: str | None,
    object_storage_endpoint: str | None,
) -> dict[str, Any]:
    volumes: list[dict[str, Any]] = [
        {
            "name": "reference-data",
            "hostPath": {"path": shared_host_path, "type": "Directory"},
        },
        {"name": "work", "emptyDir": {"sizeLimit": ephemeral_storage}},
    ]
    mounts: list[dict[str, Any]] = [
        {
            "name": "reference-data",
            "mountPath": "/reference-data",
            "readOnly": reference_data_read_only,
        },
        {"name": "work", "mountPath": "/work"},
    ]
    if telemetry_host_path:
        volumes.append({
            "name": "telemetry",
            "hostPath": {"path": telemetry_host_path, "type": "Directory"},
        })
        mounts.append({"name": "telemetry", "mountPath": "/telemetry"})
    for item in config_maps:
        volumes.append({"name": item["volume"], "configMap": {"name": item["name"], "defaultMode": 0o444}})
        mounts.append({"name": item["volume"], "mountPath": item["mount"], "readOnly": True})
    environment: list[dict[str, Any]] = []
    if credentials_secret:
        environment.extend([
            {
                "name": "AWS_ACCESS_KEY_ID",
                "valueFrom": {"secretKeyRef": {"name": credentials_secret, "key": "access-key-id"}},
            },
            {
                "name": "AWS_SECRET_ACCESS_KEY",
                "valueFrom": {"secretKeyRef": {"name": credentials_secret, "key": "secret-access-key"}},
            },
        ])
    if object_storage_endpoint:
        environment.append({"name": "AWS_ENDPOINT_URL", "value": object_storage_endpoint})
    pod_labels = {
        "app.kubernetes.io/name": "fs2-reference-data",
        "app.kubernetes.io/part-of": "fs2-serve",
        **labels,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                **pod_labels,
                "kueue.x-k8s.io/queue-name": queue,
                "kueue.x-k8s.io/priority-class": "batch",
            },
        },
        "spec": {
            "suspend": True,
            "backoffLimit": backoff_limit,
            "activeDeadlineSeconds": active_deadline_seconds,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "fs2-reference-data",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "nodeSelector": {
                        "workload.fs2.nebius/system": "true",
                        "capacity.fs2.nebius/type": "regular",
                        "capacity.fs2.nebius/pool": "system",
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "worker",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": command,
                            "env": environment,
                            "resources": {
                                "requests": {"cpu": cpu, "memory": memory, "ephemeral-storage": ephemeral_storage},
                                "limits": {"cpu": cpu, "memory": memory, "ephemeral-storage": ephemeral_storage},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def render_stage(args: argparse.Namespace) -> dict[str, Any]:
    catalog = validate_catalog(load_json(args.catalog))
    if args.bundle not in catalog["bundles"]:
        raise ContractError(f"unknown bundle id {args.bundle}")
    bundle = catalog["bundles"][args.bundle]
    access_receipt: dict[str, Any] | None = None
    if args.access_receipt:
        value = load_json(args.access_receipt)
        validate_access_receipt(value, bundle)
        access_receipt = value
    elif bundle["access"]["staging_policy"] != "automatic-public":
        raise ContractError("this bundle requires --access-receipt before a staging Job can be rendered")
    identity = hashlib.sha256(canonical_json({"bundle": args.bundle, "revision": bundle["revision"]})).hexdigest()[:12]
    config_name = f"fs2-stage-{identity}"
    config_data = {"catalog.json": json.dumps(catalog, indent=2, sort_keys=True) + "\n"}
    command = [
        "python", "/opt/fs2/reference-data/reference_data.py", "stage",
        "--catalog", "/etc/fs2-stage/catalog.json", "--bundle", args.bundle,
        "--root", "/reference-data",
    ]
    if access_receipt is not None:
        config_data["access-receipt.json"] = json.dumps(access_receipt, indent=2, sort_keys=True) + "\n"
        command.extend(["--access-receipt", "/etc/fs2-stage/access-receipt.json"])
    if args.object_store_prefix:
        command.extend(["--object-store-prefix", args.object_store_prefix])
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": args.namespace},
        "immutable": True,
        "data": config_data,
    }
    job = _base_job(
        name=f"fs2-stage-{args.bundle[:35].rstrip('-')}-{identity}",
        namespace=args.namespace,
        queue=args.queue,
        image=args.image,
        command=command,
        labels={
            "app.kubernetes.io/component": "reference-data-stager",
            "reference-data.fs2.nebius.ai/bundle": args.bundle,
            "reference-data.fs2.nebius.ai/network-mode": "public-source-staging",
        },
        cpu=args.cpu,
        memory=args.memory,
        ephemeral_storage=args.ephemeral_storage,
        active_deadline_seconds=args.active_deadline_seconds,
        backoff_limit=args.backoff_limit,
        shared_host_path=args.shared_host_path,
        reference_data_read_only=False,
        telemetry_host_path=None,
        config_maps=[
            {"volume": "tools", "name": args.tools_config_map, "mount": "/opt/fs2/reference-data"},
            {"volume": "stage-config", "name": config_name, "mount": "/etc/fs2-stage"},
        ],
        credentials_secret=args.credentials_secret,
        object_storage_endpoint=args.object_storage_endpoint,
    )
    return {"apiVersion": "v1", "kind": "List", "items": [config_map, job]}


def render_preprocess(args: argparse.Namespace) -> dict[str, Any]:
    document = validate_preprocess_request(load_json(args.request), allow_public_msa=args.allow_public_msa)
    digest = hashlib.sha256(canonical_json(document)).hexdigest()
    config_name = f"fs2-preprocess-{digest[:12]}"
    network_mode = document["privacy"]["network_mode"]
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": args.namespace},
        "immutable": True,
        "data": {"request.json": json.dumps(document, indent=2, sort_keys=True) + "\n"},
    }
    execution = document["execution"]
    command = [
        "python", "/opt/fs2/reference-data/reference_data.py", "preprocess",
        "--request", "/etc/fs2-preprocess/request.json",
        "--telemetry-root", "/telemetry",
    ]
    if args.allow_public_msa:
        command.append("--allow-public-msa")
    job = _base_job(
        name=f"fs2-preprocess-{document['request_id'][:32].rstrip('-')}-{digest[:12]}",
        namespace=args.namespace,
        queue=args.queue,
        image=execution["image"],
        command=command,
        labels={
            "app.kubernetes.io/component": "private-msa",
            "reference-data.fs2.nebius.ai/network-mode": network_mode,
            "reference-data.fs2.nebius.ai/bundle": document["reference_data"]["bundle_id"],
        },
        cpu=execution["cpu"],
        memory=execution["memory"],
        ephemeral_storage=execution["ephemeral_storage"],
        active_deadline_seconds=execution["active_deadline_seconds"],
        backoff_limit=execution["backoff_limit"],
        shared_host_path=args.shared_host_path,
        reference_data_read_only=True,
        telemetry_host_path=f"{args.shared_host_path.rstrip('/')}/telemetry",
        config_maps=[
            {"volume": "tools", "name": args.tools_config_map, "mount": "/opt/fs2/reference-data"},
            {"volume": "preprocess-config", "name": config_name, "mount": "/etc/fs2-preprocess"},
        ],
        credentials_secret=args.credentials_secret,
        object_storage_endpoint=args.object_storage_endpoint,
    )
    return {"apiVersion": "v1", "kind": "List", "items": [config_map, job]}


def _common(subparser: argparse.ArgumentParser) -> None:
    default_tools_config_map = (
        "fs2-reference-data-tools-"
        + hashlib.sha256(Path(__file__).with_name("reference_data.py").read_bytes()).hexdigest()[:12]
    )
    subparser.add_argument("--namespace", default="fs2-data", type=lambda value: _dns_label(value, "namespace"))
    subparser.add_argument("--queue", default="reference-data", type=lambda value: _dns_label(value, "queue"))
    subparser.add_argument("--tools-config-map", default=default_tools_config_map, type=lambda value: _dns_label(value, "tools ConfigMap"))
    subparser.add_argument("--shared-host-path", default="/mnt/fs2cache/csi-mounted-fs-path-data/reference-data")
    subparser.add_argument("--credentials-secret", type=lambda value: _dns_label(value, "credentials Secret"))
    subparser.add_argument("--object-storage-endpoint")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    _common(stage)
    stage.add_argument("--catalog", type=Path, required=True)
    stage.add_argument("--bundle", required=True)
    stage.add_argument("--image", required=True)
    stage.add_argument("--access-receipt", type=Path)
    stage.add_argument("--object-store-prefix")
    stage.add_argument("--cpu", default="8")
    stage.add_argument("--memory", default="32Gi")
    stage.add_argument("--ephemeral-storage", default="32Gi")
    stage.add_argument("--active-deadline-seconds", type=int, default=604800)
    stage.add_argument("--backoff-limit", type=int, default=2)
    preprocess = commands.add_parser("preprocess")
    _common(preprocess)
    preprocess.add_argument("--request", type=Path, required=True)
    preprocess.add_argument("--allow-public-msa", action="store_true")
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "stage":
            if not re.fullmatch(r"^[^@\s]+@sha256:[a-f0-9]{64}$", args.image):
                raise ContractError("staging image must be digest-pinned")
            document = render_stage(args)
        else:
            document = render_preprocess(args)
        json.dump(document, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
