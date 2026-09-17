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
from datetime import UTC, datetime
from pathlib import Path

from verify_owner_identity import _kubectl


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--node-name", required=True)
    parser.add_argument("--lane-id", required=True)
    parser.add_argument("--scheduling-key", required=True)
    args = parser.parse_args()

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
    taints = node.get("spec", {}).get("taints", [])
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
    ):
        raise ValueError("live Node does not match the stable protected-lane identity")

    attestation = {
        "schema": "fs2-serve.nebius.ai/protected-lane-node-attestation/v1",
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "lane_id": args.lane_id,
        "scheduling_key": args.scheduling_key,
        "nodes": {
            args.node_name: {
                "name": args.node_name,
                "uid": metadata["uid"],
                "resource_version": metadata["resourceVersion"],
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
