#!/usr/bin/env python3
"""Render immutable-by-contract additive SAI-07 epoch admission generations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA = "fs2-serve.nebius.ai/sai07-custody-epoch-admission/v4"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@/\-]{0,252}$")


class EpochAdmissionError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def exact(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EpochAdmissionError(f"{label} fields differ from the v4 contract")
    return value


def identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not IDENTITY_RE.fullmatch(value):
        raise EpochAdmissionError(f"{label} is not an exact safe identity")
    return value


def epoch(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise EpochAdmissionError(f"{label} is not a lowercase SHA-256")
    return value


def policy_name(prefix: str, epoch_sha256: str) -> str:
    return f"fs2-pod-security-{prefix}-v4-{epoch_sha256}"


def current_policy(item: dict[str, str]) -> list[dict[str, Any]]:
    epoch_sha256 = item["epoch_sha256"]
    owner = item["owner_username"]
    receipt = item["receipt_username"]
    anchor = f"fs2-pod-security-token-anchor-v4-{epoch_sha256}"
    name = policy_name("custody-epoch", epoch_sha256)
    expression = (
        f"request.userInfo.username == '{owner}' ? "
        f"(object.kind == 'Secret' ? (request.operation == 'CREATE' && object.metadata.namespace == 'fs2-system' && object.metadata.name == '{anchor}' && object.immutable == true && object.type == 'Opaque' && object.data == {{}} && !has(object.stringData) && object.metadata.annotations == {{'security.fs2.nebius.ai/custody-epoch-sha256':'{epoch_sha256}'}}) : "
        f"object.kind == 'ConfigMap' ? (request.operation == 'CREATE' && object.metadata.namespace == 'fs2-system' && object.metadata.name.startsWith('fs2-sai07-custody-ack-v3-') && object.immutable == true && object.metadata.annotations['security.fs2.nebius.ai/custody-epoch-sha256'] == '{epoch_sha256}') : false) : "
        f"request.userInfo.username == '{receipt}' ? (object.kind == 'TokenRequest' && request.operation == 'CREATE' && object.spec.audiences == ['https://kubernetes.default.svc'] && object.spec.expirationSeconds <= 600 && has(object.spec.boundObjectRef) && object.spec.boundObjectRef.apiVersion == 'v1' && object.spec.boundObjectRef.kind == 'Secret' && object.spec.boundObjectRef.name == '{anchor}' && object.spec.boundObjectRef.uid != '') : false"
    )
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {
            "annotations": {
                "security.fs2.nebius.ai/custody-epoch-sha256": epoch_sha256,
                "security.fs2.nebius.ai/owner-username-sha256": hashlib.sha256(
                    owner.encode()
                ).hexdigest(),
                "security.fs2.nebius.ai/receipt-username-sha256": hashlib.sha256(
                    receipt.encode()
                ).hexdigest(),
            },
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-sai07-external-custody",
                "security.fs2.nebius.ai/role": "current-epoch-admission",
            },
            "name": name,
        },
        "spec": {
            "failurePolicy": "Fail",
            "matchConditions": [
                {
                    "expression": f"request.userInfo.username in ['{owner}','{receipt}']",
                    "name": "exact-epoch-identities",
                }
            ],
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE"],
                        "resources": ["configmaps", "secrets", "serviceaccounts/token"],
                    }
                ]
            },
            "validations": [
                {
                    "expression": expression,
                    "message": "The epoch identities may create only their exact anchor, acknowledgement, or bound token.",
                }
            ],
        },
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {
            "annotations": {
                "security.fs2.nebius.ai/custody-epoch-sha256": epoch_sha256
            },
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-sai07-external-custody",
                "security.fs2.nebius.ai/role": "current-epoch-admission",
            },
            "name": name,
        },
        "spec": {"policyName": name, "validationActions": ["Deny"]},
    }
    return [policy, binding]


def retired_policy(item: dict[str, str]) -> list[dict[str, Any]]:
    epoch_sha256 = item["epoch_sha256"]
    owner = item["owner_username"]
    receipt = item["receipt_username"]
    name = policy_name("custody-retired", epoch_sha256)
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {
            "annotations": {
                "security.fs2.nebius.ai/custody-epoch-sha256": epoch_sha256,
                "security.fs2.nebius.ai/owner-username-sha256": hashlib.sha256(
                    owner.encode()
                ).hexdigest(),
                "security.fs2.nebius.ai/receipt-username-sha256": hashlib.sha256(
                    receipt.encode()
                ).hexdigest(),
            },
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-sai07-external-custody",
                "security.fs2.nebius.ai/role": "retired-epoch-deny",
            },
            "name": name,
        },
        "spec": {
            "failurePolicy": "Fail",
            "matchConditions": [
                {
                    "expression": f"request.userInfo.username in ['{owner}','{receipt}']",
                    "name": "retired-epoch-identities",
                }
            ],
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": ["*"],
                        "apiVersions": ["*"],
                        "operations": ["CREATE", "UPDATE", "DELETE", "CONNECT"],
                        "resources": ["*"],
                    }
                ]
            },
            "validations": [
                {
                    "expression": "false",
                    "message": "This preserved custody epoch is retired and has no Kubernetes write authority.",
                }
            ],
        },
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {
            "annotations": {
                "security.fs2.nebius.ai/custody-epoch-sha256": epoch_sha256
            },
            "labels": {
                "app.kubernetes.io/managed-by": "fs2-sai07-external-custody",
                "security.fs2.nebius.ai/role": "retired-epoch-deny",
            },
            "name": name,
        },
        "spec": {"policyName": name, "validationActions": ["Deny"]},
    }
    return [policy, binding]


def render(contract: dict[str, Any]) -> list[dict[str, Any]]:
    contract = exact(
        contract,
        {"activation", "current", "retained_anchor_names", "retired", "schema", "status"},
        "epoch admission contract",
    )
    if contract["activation"] != "active" or contract["schema"] != SCHEMA:
        raise EpochAdmissionError("epoch admission contract is blocked")
    current = exact(
        contract["current"],
        {"epoch_sha256", "owner_username", "receipt_username"},
        "current epoch",
    )
    current = {
        "epoch_sha256": epoch(current["epoch_sha256"], "current epoch"),
        "owner_username": identity(current["owner_username"], "current owner"),
        "receipt_username": identity(
            current["receipt_username"], "current receipt operator"
        ),
    }
    if current["owner_username"] == current["receipt_username"]:
        raise EpochAdmissionError("current owner and receipt identities overlap")
    retired = contract["retired"]
    retained_anchor_names = contract["retained_anchor_names"]
    if (
        not isinstance(retained_anchor_names, list)
        or retained_anchor_names != sorted(set(retained_anchor_names))
        or f"fs2-pod-security-token-anchor-v4-{current['epoch_sha256']}"
        not in retained_anchor_names
        or any(
            not isinstance(name, str)
            or re.fullmatch(
                r"fs2-pod-security-token-anchor-v(?:3|4)-[a-f0-9]{64}", name
            )
            is None
            for name in retained_anchor_names
        )
    ):
        raise EpochAdmissionError("retained anchor inventory is not exact/canonical")
    if not isinstance(retired, list):
        raise EpochAdmissionError("retired epoch inventory is not a list")
    normalized: list[dict[str, str]] = []
    for index, value in enumerate(retired):
        item = exact(
            value,
            {"epoch_sha256", "owner_username", "receipt_username"},
            f"retired epoch {index}",
        )
        normalized.append(
            {
                "epoch_sha256": epoch(item["epoch_sha256"], "retired epoch"),
                "owner_username": identity(item["owner_username"], "retired owner"),
                "receipt_username": identity(
                    item["receipt_username"], "retired receipt operator"
                ),
            }
        )
    if normalized != sorted(normalized, key=lambda item: item["epoch_sha256"]):
        raise EpochAdmissionError("retired epochs are not canonical")
    all_epochs = [current["epoch_sha256"], *[item["epoch_sha256"] for item in normalized]]
    all_identities = [
        current["owner_username"],
        current["receipt_username"],
        *[identity for item in normalized for identity in (item["owner_username"], item["receipt_username"])],
    ]
    if len(all_epochs) != len(set(all_epochs)) or len(all_identities) != len(
        set(all_identities)
    ):
        raise EpochAdmissionError("epoch digests or Kubernetes identities are reused")
    manifests = current_policy(current)
    for item in normalized:
        manifests.extend(retired_policy(item))
    return manifests
