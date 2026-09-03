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
    check_execution_fits,
    load_json,
    load_placement_contract,
    resolve_stage_placement,
    validate_access_receipt,
    validate_catalog,
    validate_preprocess_request,
)


DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _dns_label(value: str, context: str) -> str:
    if len(value) > 63 or not DNS_LABEL_RE.fullmatch(value):
        raise ContractError(f"{context} must be a Kubernetes DNS label")
    return value


def _reference_namespace(value: str) -> str:
    namespace = _dns_label(value, "namespace")
    if namespace != "fs2-reference-data":
        raise ContractError(
            "reference-data Jobs must use the dedicated fs2-reference-data namespace; "
            "fs2-data contains the live database and is forbidden"
        )
    return namespace


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
    node_selector: Mapping[str, str],
    tolerations: Sequence[Mapping[str, str]],
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
                    "nodeSelector": dict(node_selector),
                    "tolerations": [dict(item) for item in tolerations],
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


def _placement(args: argparse.Namespace, stage_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one stage's placement and sizing from the tfvars-derived contract."""
    contract = load_placement_contract(getattr(args, "placement", None))
    return contract, resolve_stage_placement(contract, stage_id)


def _sized_execution(placement: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Apply explicit overrides on top of the contract's stage defaults."""
    execution = dict(placement["defaults"])
    for key, value in overrides.items():
        if value is not None:
            execution[key] = value
    return execution


def render_stage(args: argparse.Namespace) -> dict[str, Any]:
    namespace = _reference_namespace(args.namespace)
    contract, placement = _placement(args, "staging")
    execution = _sized_execution(placement, {
        "cpu": getattr(args, "cpu", None),
        "memory": getattr(args, "memory", None),
        "ephemeral_storage": getattr(args, "ephemeral_storage", None),
        "active_deadline_seconds": getattr(args, "active_deadline_seconds", None),
        "backoff_limit": getattr(args, "backoff_limit", None),
    })
    check_execution_fits(execution, contract, "staging")
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
        "metadata": {"name": config_name, "namespace": namespace},
        "immutable": True,
        "data": config_data,
    }
    job = _base_job(
        name=f"fs2-stage-{args.bundle[:35].rstrip('-')}-{identity}",
        namespace=namespace,
        queue=args.queue,
        image=args.image,
        command=command,
        labels={
            "app.kubernetes.io/component": "reference-data-stager",
            "reference-data.fs2.nebius.ai/bundle": args.bundle,
            "reference-data.fs2.nebius.ai/network-mode": "public-source-staging",
        },
        cpu=execution["cpu"],
        memory=execution["memory"],
        ephemeral_storage=execution["ephemeral_storage"],
        active_deadline_seconds=execution["active_deadline_seconds"],
        backoff_limit=execution["backoff_limit"],
        shared_host_path=args.shared_host_path,
        reference_data_read_only=False,
        node_selector=placement["node_selector"],
        tolerations=placement["tolerations"],
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
    namespace = _reference_namespace(args.namespace)
    contract, placement = _placement(args, "raw-input")
    document = validate_preprocess_request(load_json(args.request), allow_public_msa=args.allow_public_msa)
    check_execution_fits(document["execution"], contract, "raw-input")
    digest = hashlib.sha256(canonical_json(document)).hexdigest()
    config_name = f"fs2-preprocess-{digest[:12]}"
    network_mode = document["privacy"]["network_mode"]
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": config_name, "namespace": namespace},
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
        namespace=namespace,
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
        node_selector=placement["node_selector"],
        tolerations=placement["tolerations"],
        telemetry_host_path=f"{args.shared_host_path.rstrip('/')}/telemetry",
        config_maps=[
            {"volume": "tools", "name": args.tools_config_map, "mount": "/opt/fs2/reference-data"},
            {"volume": "preprocess-config", "name": config_name, "mount": "/etc/fs2-preprocess"},
        ],
        credentials_secret=args.credentials_secret,
        object_storage_endpoint=args.object_storage_endpoint,
    )
    return {"apiVersion": "v1", "kind": "List", "items": [config_map, job]}


ROUTE_SCHEMA = "fs2-serve.nebius.ai/reference-data-stage-route/v1"


def render_route(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the two independently placed stages of a raw-input request.

    The data pipeline is a CPU Job on the dedicated reference pool. Inference
    is a separate stage placed by accelerator flavor, and it consumes only the
    immutable digests the data stage publishes, so no accelerator is ever
    allocated while reference databases are being read.
    """
    contract, raw_input = _placement(args, "raw-input")
    inference = resolve_stage_placement(contract, "inference")
    document = validate_preprocess_request(load_json(args.request), allow_public_msa=args.allow_public_msa)
    check_execution_fits(document["execution"], contract, "raw-input")
    check_execution_fits(inference["defaults"], contract, "inference")
    if "accelerator" in raw_input:
        raise ContractError("the raw-input stage must not reserve an accelerator")
    rendered = render_preprocess(args)
    request_sha256 = hashlib.sha256(canonical_json(document)).hexdigest()
    return {
        "schema": ROUTE_SCHEMA,
        "request_id": document["request_id"],
        "request_sha256": request_sha256,
        "reference_data": dict(document["reference_data"]),
        "stages": [
            {
                "id": "raw-input",
                "resource_class": raw_input["resource_class"],
                "pool": raw_input["pool"],
                "queue": raw_input["queue"],
                "node_selector": raw_input["node_selector"],
                "tolerations": raw_input["tolerations"],
                "resources": {
                    "cpu": document["execution"]["cpu"],
                    "memory": document["execution"]["memory"],
                    "ephemeral_storage": document["execution"]["ephemeral_storage"],
                },
                "job": rendered["items"][1]["metadata"]["name"],
                "produces": {
                    "result_schema": "fs2-serve.nebius.ai/private-preprocess-result/v1",
                    "output_prefix_uri": document["output"]["prefix_uri"],
                    "binds": ["request_sha256", "result_manifest_sha256"],
                },
            },
            {
                "id": "inference",
                "resource_class": inference["resource_class"],
                "pool": inference["pool"],
                "queue": inference["queue"],
                "node_selector": inference["node_selector"],
                "tolerations": inference["tolerations"],
                "accelerator": inference["accelerator"],
                "resources": {
                    "cpu": inference["defaults"]["cpu"],
                    "memory": inference["defaults"]["memory"],
                    "ephemeral_storage": inference["defaults"]["ephemeral_storage"],
                },
                "needs": ["raw-input"],
                "consumes": {
                    "handoff_schema": "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1",
                    "binds": [
                        "storage.host_root",
                        "storage.dataset_sub_path",
                        "content.tree_sha256",
                        "content.manifest_sha256",
                        "content.inventory_sha256",
                        "content.file_count",
                        "content.expanded_bytes",
                    ],
                    "reference_database_download": "prohibited",
                },
            },
        ],
        "resources": rendered,
    }


def _common(subparser: argparse.ArgumentParser) -> None:
    default_tools_config_map = (
        "fs2-reference-data-tools-"
        + hashlib.sha256(Path(__file__).with_name("reference_data.py").read_bytes()).hexdigest()[:12]
    )
    subparser.add_argument("--namespace", default="fs2-reference-data", type=_reference_namespace)
    subparser.add_argument("--queue", default="reference-data", type=lambda value: _dns_label(value, "queue"))
    subparser.add_argument("--tools-config-map", default=default_tools_config_map, type=lambda value: _dns_label(value, "tools ConfigMap"))
    subparser.add_argument("--shared-host-path", default="/mnt/fs2-reference-data/data")
    subparser.add_argument("--placement", type=Path)
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
    stage.add_argument("--cpu")
    stage.add_argument("--memory")
    stage.add_argument("--ephemeral-storage")
    stage.add_argument("--active-deadline-seconds", type=int)
    stage.add_argument("--backoff-limit", type=int)
    preprocess = commands.add_parser("preprocess")
    _common(preprocess)
    preprocess.add_argument("--request", type=Path, required=True)
    preprocess.add_argument("--allow-public-msa", action="store_true")
    route = commands.add_parser("route")
    _common(route)
    route.add_argument("--request", type=Path, required=True)
    route.add_argument("--allow-public-msa", action="store_true")
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "stage":
            if not re.fullmatch(r"^[^@\s]+@sha256:[a-f0-9]{64}$", args.image):
                raise ContractError("staging image must be digest-pinned")
            document = render_stage(args)
        elif args.command == "route":
            document = render_route(args)
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
