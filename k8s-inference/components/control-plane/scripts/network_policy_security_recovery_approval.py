#!/usr/bin/env python3
"""Create a short-lived, live-state-bound admission recovery approval."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from network_policy_security_enforcer import (
    ADMISSION_NAME,
    PARAMETER_NAME,
    RECOVERY_APPROVAL_SCHEMA,
    RELEASE_NAMESPACE,
    STATE_NAME,
    TOPOLOGY_NAME,
    EnforcerError,
    KubectlAPI,
    canonical,
    load_private_key,
    object_evidence,
    receipt_from_object,
    sha256_json,
    sha256_text,
)


def recovery_object_evidence(resource_object: dict[str, Any]) -> dict[str, Any]:
    evidence = object_evidence(resource_object)
    evidence["spec"] = resource_object.get("spec")
    evidence["data"] = resource_object.get("data")
    return evidence


def create_approval(
    api: KubectlAPI,
    *,
    private_key_path: Path,
    mode: str,
    recovery_reference: str,
) -> dict[str, Any]:
    """Sign the exact live topology and receipt; never mutate Kubernetes state."""
    topology_object = api.get("configmap", TOPOLOGY_NAME, RELEASE_NAMESPACE)
    receipt_object = api.get("configmap", STATE_NAME, RELEASE_NAMESPACE)
    binding = api.get("validatingadmissionpolicybinding", ADMISSION_NAME)
    parameter = api.get("configmap", PARAMETER_NAME, RELEASE_NAMESPACE)
    try:
        topology = json.loads(topology_object.get("data", {}).get("topology.json", ""))
    except json.JSONDecodeError as error:
        raise EnforcerError("protected topology is not valid JSON") from error
    if not isinstance(topology, dict):
        raise EnforcerError("protected topology is not an object")
    handoff = topology.get("security_handoff", {})
    cluster = api.cluster_identity()
    if cluster != handoff.get("cluster"):
        raise EnforcerError("security approval is connected to a different cluster")
    private_key = load_private_key(private_key_path)
    public_value = base64.urlsafe_b64encode(private_key.public_key().public_bytes_raw()).decode().rstrip("=")
    key_id = sha256_text(public_value)
    if (
        key_id != handoff.get("recovery_public_key_sha256")
        or parameter.get("data", {}).get("recovery_signer_key_id") != key_id
        or parameter.get("data", {}).get("delete_allowed") != "false"
    ):
        raise EnforcerError("recovery signer is not pinned by the permanent boundary")
    receipt = receipt_from_object(receipt_object)
    topology_metadata = topology_object.get("metadata", {})
    receipt_metadata = receipt_object.get("metadata", {})
    issued = dt.datetime.now(dt.UTC)
    signed = {
        "schema": RECOVERY_APPROVAL_SCHEMA,
        "mode": mode,
        "recovery_reference": recovery_reference,
        "cluster": cluster,
        "topology_uid": topology_metadata.get("uid"),
        "topology_sha256": sha256_json(topology),
        "receipt_uid": receipt_metadata.get("uid"),
        "receipt_resource_version": receipt_metadata.get("resourceVersion"),
        "receipt_sha256": sha256_json(receipt),
        "binding": recovery_object_evidence(binding),
        "parameter": recovery_object_evidence(parameter),
        "issued_at": issued.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "expires_at": (issued + dt.timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "signer_key_id": key_id,
    }
    if not all(signed.get(field) for field in ("topology_uid", "receipt_uid", "receipt_resource_version")):
        raise EnforcerError("live recovery identity is incomplete")
    signature = base64.urlsafe_b64encode(private_key.sign(canonical(signed).encode())).decode().rstrip("=")
    return {"signed": signed, "signature": signature}


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--security-kubeconfig", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--recovery-private-key", required=True)
    parser.add_argument("--mode", choices=("audit-warn", "deny"), required=True)
    parser.add_argument("--recovery-reference", required=True)
    arguments = parser.parse_args(argv)
    if not re.fullmatch(r"(?:SEC|INC|CHG)-[1-9][0-9]{2,15}", arguments.recovery_reference):
        parser.error("recovery reference must be an exact SEC, INC, or CHG record")
    for field in ("security_kubeconfig", "recovery_private_key"):
        if not Path(getattr(arguments, field)).is_absolute():
            parser.error(f"{field} path must be absolute")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv or sys.argv[1:])
    api = KubectlAPI(Path(arguments.security_kubeconfig), arguments.context)
    try:
        approval = create_approval(
            api,
            private_key_path=Path(arguments.recovery_private_key),
            mode=arguments.mode,
            recovery_reference=arguments.recovery_reference,
        )
    except EnforcerError as error:
        print(f"security recovery approval failed closed: {error}", file=sys.stderr)
        return 1
    print(canonical(approval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
