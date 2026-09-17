#!/usr/bin/env python3
"""Emit an unsigned post-provisioning protected-lane Node attestation.

The stable provider lane and its immutable taint key must exist before this
read-only collector runs.  An external security owner signs the emitted bytes;
the boundary then installs its Node mutation guard and re-reads the live Node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from verify_owner_identity import _kubectl

AUTHORITY_ROOT = Path(__file__).resolve().parents[1] / "customer-storage-egress-authority"
if os.fspath(AUTHORITY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(AUTHORITY_ROOT))

from verify_authority_ledger import (  # noqa: E402
    REGISTRY_PATH,
    require_fresh_timestamp,
    safe_root_read_with_identity,
    strict_json,
    verify_signed_object,
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--node-name", required=True)
    parser.add_argument("--lane-id", required=True)
    parser.add_argument("--scheduling-key", required=True)
    parser.add_argument("--provisioning-generation", required=True)
    args = parser.parse_args()

    registry_payload, _ = safe_root_read_with_identity(REGISTRY_PATH)
    registry = strict_json(registry_payload, "authority registry")
    receipts = registry.get("lane_provisioning_receipts")
    receipt = (
        receipts.get(args.provisioning_generation)
        if isinstance(receipts, dict)
        else None
    )
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema")
        != "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v2"
    ):
        raise ValueError("fresh signed provisioning receipt is absent")
    receipt_sha256 = verify_signed_object(
        receipt, public_key_pem=str(registry["checkpoint_public_key_pem"])
    )
    require_fresh_timestamp(receipt.get("observed_at"), "lane provisioning receipt")
    provider_inventory = receipt.get("provider_inventory")
    node_group = (
        provider_inventory.get("node_group")
        if isinstance(provider_inventory, dict)
        else None
    )
    members = node_group.get("members") if isinstance(node_group, dict) else None
    if (
        not isinstance(members, list)
        or receipt.get("provisioning_generation") != args.provisioning_generation
        or node_group.get("id") != receipt.get("node_group_id")
    ):
        raise ValueError("signed NodeGroup membership is incomplete")

    node = json.loads(
        _kubectl(
            args.kubeconfig,
            args.context,
            "get",
            "node",
            args.node_name,
            "-o",
            "json",
        )
    )
    metadata = node.get("metadata", {})
    labels = metadata.get("labels", {})
    node_spec = node.get("spec", {})
    taints = node_spec.get("taints", [])
    provider_id = node_spec.get("providerID")
    expected_taint = {
        "key": args.scheduling_key,
        "value": args.lane_id,
        "effect": "NoSchedule",
    }
    if (
        metadata.get("name") != args.node_name
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
        or not isinstance(metadata.get("resourceVersion"), str)
        or not metadata["resourceVersion"]
        or not isinstance(labels, dict)
        or labels.get(args.scheduling_key) != args.lane_id
        or not isinstance(taints, list)
        or expected_taint not in taints
        or not isinstance(provider_id, str)
        or not provider_id
    ):
        raise ValueError("live Node does not match the stable protected-lane identity")
    member = [item for item in members if item.get("provider_id") == provider_id]
    if (
        len(member) != 1
        or member[0].get("node_group_id") != receipt.get("node_group_id")
    ):
        raise ValueError("Kubernetes providerID is not a signed member of the lane NodeGroup")

    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    attestation = {
        "schema": "fs2-serve.nebius.ai/protected-lane-node-attestation/v2",
        "observed_at": observed_at,
        "lane_id": args.lane_id,
        "scheduling_key": args.scheduling_key,
        "provisioning_generation": args.provisioning_generation,
        "provisioning_receipt_sha256": receipt_sha256,
        "node_group_id": receipt["node_group_id"],
        "nodes": {
            args.node_name: {
                "name": args.node_name,
                "uid": metadata["uid"],
                "resource_version": metadata["resourceVersion"],
                "provider_id": provider_id,
                "node_group_id": receipt["node_group_id"],
                "provisioning_receipt_sha256": receipt_sha256,
                "observed_at": observed_at,
                "labels": dict(sorted(labels.items())),
                # Preserve the API's exact list order because the mutation
                # guard and post-install read compare the complete live value.
                "taints": taints,
            }
        },
    }
    attestation["payload_sha256"] = hashlib.sha256(canonical(attestation)).hexdigest()
    print(json.dumps(attestation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
