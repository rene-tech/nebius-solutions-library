#!/usr/bin/env python3
"""Create non-secret receipts for the phased fs2-models NetworkPolicy rollout.

The tool never changes Kubernetes. It reads a Terraform transition contract and
the live API, then emits a content-addressed receipt only when every Deployment
has the immutable runtime/profile labels required by the finite policies, or
when rollback has already removed default-deny while retaining those policies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
COMPONENT_LABEL = "app.kubernetes.io/component"
PART_OF_LABEL = "app.kubernetes.io/part-of"
NAMESPACE = "fs2-models"


class ReceiptError(ValueError):
    """The live inventory cannot authorize the requested transition."""


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{field} must be a JSON object")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ReceiptError(f"{field} must be a non-empty-string JSON array")
    if value != sorted(set(value)):
        raise ReceiptError(f"{field} must be sorted and duplicate-free")
    return value


def _sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read JSON from {path}: {exc}") from exc


def _captured_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("--captured-at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReceiptError("--captured-at must include a timezone")
    return value


def _kubectl_json(*, kubectl: str, kubeconfig: Path, context: str, resource: str) -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - operator-selected kubectl with fixed arguments
        [
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            context,
            "get",
            resource,
            "--namespace",
            NAMESPACE,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "kubectl returned no diagnostic"
        raise ReceiptError(f"kubectl inventory failed: {detail}")
    try:
        return _object(json.loads(result.stdout), f"kubectl get {resource}")
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"kubectl get {resource} returned invalid JSON") from exc


def _contract(contract: dict[str, Any], *, phase: str) -> dict[str, Any]:
    if contract.get("phase") != phase:
        raise ReceiptError(f"transition contract must be in {phase!r} phase")
    if contract.get("namespace") != NAMESPACE:
        raise ReceiptError(f"transition contract namespace must be {NAMESPACE}")
    profiles = _strings(contract.get("profiles"), "contract.profiles")
    policy_names = _strings(contract.get("allow_policy_names"), "contract.allow_policy_names")
    expected_profile_digest = hashlib.sha256(json.dumps(profiles, separators=(",", ":")).encode()).hexdigest()
    if contract.get("profiles_sha256") != expected_profile_digest:
        raise ReceiptError("transition contract profile digest is inconsistent")
    if policy_names != [f"fs2-runtime-profile-{profile}" for profile in profiles]:
        raise ReceiptError("transition contract policy names do not match its profiles")
    image = _object(contract.get("control_plane_image"), "contract.control_plane_image")
    if not isinstance(image.get("repository"), str) or not image["repository"]:
        raise ReceiptError("control-plane image repository is missing")
    digest = image.get("digest")
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
        raise ReceiptError("control-plane image digest is not immutable")
    return contract


def inventory_receipt(contract: dict[str, Any], resources: dict[str, Any], *, captured_at: str) -> dict[str, Any]:
    contract = _contract(contract, phase="prepare")
    items = resources.get("items")
    if not isinstance(items, list) or not items:
        raise ReceiptError("live fs2-models Deployment inventory is empty")
    recognized = set(contract["profiles"])
    deployments: dict[str, dict[str, Any]] = {}
    for raw in items:
        item = _object(raw, "deployment")
        metadata = _object(item.get("metadata"), "deployment.metadata")
        spec = _object(item.get("spec"), "deployment.spec")
        template = _object(spec.get("template"), "deployment.spec.template")
        template_metadata = _object(template.get("metadata"), "deployment.spec.template.metadata")
        labels = _object(metadata.get("labels"), "deployment.metadata.labels")
        pod_labels = _object(template_metadata.get("labels"), "deployment.spec.template.metadata.labels")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not isinstance(name, str) or not name or name in deployments:
            raise ReceiptError("Deployment names must be non-empty and unique")
        if not isinstance(uid, str) or not uid:
            raise ReceiptError(f"Deployment {name} has no live UID")
        profile = labels.get(PROFILE_LABEL)
        if (
            labels.get(COMPONENT_LABEL) != "model-runtime"
            or labels.get(PART_OF_LABEL) != "fs2-serve"
            or pod_labels.get(COMPONENT_LABEL) != "model-runtime"
            or pod_labels.get(PART_OF_LABEL) != "fs2-serve"
            or profile not in recognized
            or pod_labels.get(PROFILE_LABEL) != profile
        ):
            raise ReceiptError(
                f"Deployment {name} does not have one recognized immutable "
                "runtime profile on both workload and Pod template"
            )
        deployments[name] = {
            "uid": uid,
            "profile": profile,
            "workload_component": labels[COMPONENT_LABEL],
            "workload_part_of": labels[PART_OF_LABEL],
            "pod_component": pod_labels[COMPONENT_LABEL],
            "pod_part_of": pod_labels[PART_OF_LABEL],
        }
    payload = {
        "schema": "fs2-serve.nebius.ai/model-runtime-network-inventory/v1",
        "cluster_id": contract.get("cluster_id"),
        "namespace": NAMESPACE,
        "captured_at": captured_at,
        "control_plane_image": contract["control_plane_image"],
        "profiles_sha256": contract["profiles_sha256"],
        "deployments": dict(sorted(deployments.items())),
    }
    if not isinstance(payload["cluster_id"], str) or not payload["cluster_id"]:
        raise ReceiptError("transition contract cluster_id is missing")
    return {**payload, "payload_sha256": _sha256(payload)}


def deny_absent_receipt(contract: dict[str, Any], resources: dict[str, Any], *, captured_at: str) -> dict[str, Any]:
    contract = _contract(contract, phase="rollback-remove-deny")
    enforcement_digest = contract.get("inventory_receipt_sha256")
    if not isinstance(enforcement_digest, str) or len(enforcement_digest) != 64:
        raise ReceiptError("rollback contract has no enforcement receipt digest")
    items = resources.get("items")
    if not isinstance(items, list):
        raise ReceiptError("live NetworkPolicy inventory has no items array")
    names: list[str] = []
    for raw in items:
        metadata = _object(_object(raw, "network policy").get("metadata"), "metadata")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ReceiptError("live NetworkPolicy has no name")
        names.append(name)
    if "default-deny" in names:
        raise ReceiptError("default-deny is still present; apply deny removal first")
    missing = sorted(set(contract["allow_policy_names"]) - set(names))
    if missing:
        raise ReceiptError(f"finite allow policies are missing: {', '.join(missing)}")
    payload = {
        "schema": "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v1",
        "cluster_id": contract.get("cluster_id"),
        "namespace": NAMESPACE,
        "captured_at": captured_at,
        "enforcement_payload_sha256": enforcement_digest,
        "profiles_sha256": contract["profiles_sha256"],
        "allow_policy_names": contract["allow_policy_names"],
        "default_deny_absent": True,
    }
    if not isinstance(payload["cluster_id"], str) or not payload["cluster_id"]:
        raise ReceiptError("transition contract cluster_id is missing")
    return {**payload, "payload_sha256": _sha256(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "deny-absent"):
        child = subparsers.add_parser(command)
        child.add_argument("--contract", required=True, type=Path)
        child.add_argument("--kubeconfig", required=True, type=Path)
        child.add_argument("--context", required=True)
        child.add_argument("--kubectl", default="kubectl")
        child.add_argument("--captured-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = _load(args.contract)
        captured_at = _captured_at(args.captured_at)
        if args.command == "inventory":
            resources = _kubectl_json(
                kubectl=args.kubectl,
                kubeconfig=args.kubeconfig,
                context=args.context,
                resource="deployments.apps",
            )
            receipt = inventory_receipt(contract, resources, captured_at=captured_at)
        else:
            resources = _kubectl_json(
                kubectl=args.kubectl,
                kubeconfig=args.kubeconfig,
                context=args.context,
                resource="networkpolicies.networking.k8s.io",
            )
            receipt = deny_absent_receipt(contract, resources, captured_at=captured_at)
    except ReceiptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
