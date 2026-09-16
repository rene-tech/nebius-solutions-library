#!/usr/bin/env python3
"""Capture the fixed live predecessor as a value-free compatibility receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from verify_owner_identity import _kubectl

NAMESPACE = "fs2-system"


def _get(kubeconfig: Path, context: str, resource: str, name: str) -> dict[str, Any]:
    value = json.loads(
        _kubectl(
            kubeconfig,
            context,
            "get",
            resource,
            name,
            "--namespace",
            NAMESPACE,
            "--output",
            "json",
        )
    )
    if not isinstance(value, dict):
        raise ValueError(f"live {resource}/{name} is not an object")
    return value


def _cluster_get(
    kubeconfig: Path, context: str, resource: str, name: str
) -> dict[str, Any]:
    value = json.loads(
        _kubectl(
            kubeconfig,
            context,
            "get",
            resource,
            name,
            "--output",
            "json",
        )
    )
    if not isinstance(value, dict):
        raise ValueError(f"live {resource}/{name} is not an object")
    return value


def _digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def capture(kubeconfig: Path, context: str) -> dict[str, Any]:
    deployment = _get(
        kubeconfig, context, "deployment", "fs2-serve-control-plane-storage-reconciler"
    )
    policy = _get(
        kubeconfig, context, "networkpolicy", "fs2-serve-control-plane-storage-reconciler"
    )
    contract = _get(
        kubeconfig, context, "configmap", "fs2-customer-storage-egress-contract"
    )
    admission = _cluster_get(
        kubeconfig,
        context,
        "validatingadmissionpolicy",
        "fs2-customer-storage-egress",
    )
    binding = _cluster_get(
        kubeconfig,
        context,
        "validatingadmissionpolicybinding",
        "fs2-customer-storage-egress",
    )
    if (
        deployment.get("spec", {})
        .get("selector", {})
        .get("matchLabels", {})
        .get("app.kubernetes.io/component")
        != "storage-reconciler"
        or policy.get("spec", {})
        .get("podSelector", {})
        .get("matchLabels", {})
        .get("app.kubernetes.io/component")
        != "storage-reconciler"
    ):
        raise ValueError("fixed predecessor selectors differ")
    if binding.get("spec") != {
        "matchResources": {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}
            }
        },
        "policyName": "fs2-customer-storage-egress",
        "validationActions": ["Deny"],
    }:
        raise ValueError("fixed predecessor binding differs")
    admission_text = json.dumps(admission.get("spec", {}), sort_keys=True)
    if (
        "storage-reconciler" not in admission_text
        or "storage-reconciler-v2" in admission_text
        or "networkpolicies" not in admission_text
        or "request.operation != 'DELETE'" not in admission_text
    ):
        raise ValueError("fixed predecessor VAP compatibility semantics differ")

    receipt = {
        "schema": "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1",
        "namespace": NAMESPACE,
        "predecessor_label": "storage-reconciler",
        "successor_label": "storage-reconciler-v2",
        "deployment": {
            "name": deployment["metadata"]["name"],
            "uid": deployment["metadata"]["uid"],
            "spec_sha256": _digest(deployment["spec"]),
        },
        "network_policy": {
            "name": policy["metadata"]["name"],
            "uid": policy["metadata"]["uid"],
            "spec_sha256": _digest(policy["spec"]),
        },
        "contract": {
            "name": contract["metadata"]["name"],
            "uid": contract["metadata"]["uid"],
            "data_sha256": _digest(contract["data"]),
        },
        "policy": {
            "name": admission["metadata"]["name"],
            "uid": admission["metadata"]["uid"],
            "spec_sha256": _digest(admission["spec"]),
        },
        "binding": {
            "name": binding["metadata"]["name"],
            "uid": binding["metadata"]["uid"],
            "spec_sha256": _digest(binding["spec"]),
        },
    }
    return {"receipt": receipt, "receipt_sha256": _digest(receipt)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(capture(args.kubeconfig, args.context), sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"predecessor compatibility receipt rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
