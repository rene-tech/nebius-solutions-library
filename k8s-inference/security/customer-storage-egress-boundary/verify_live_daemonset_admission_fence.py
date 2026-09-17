#!/usr/bin/env python3
"""Re-prove the external continuous fence before ordinary binding creation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

AUTHORITY_ROOT = Path(__file__).resolve().parents[1] / "customer-storage-egress-authority"
if os.fspath(AUTHORITY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(AUTHORITY_ROOT))

from verify_daemonset_admission_fence import (  # noqa: E402
    verify_live_daemonset_admission_fence,
)


def main() -> int:
    query = json.load(sys.stdin)
    if not isinstance(query, dict) or set(query) != {
        "cluster_id",
        "expected_inventory_sha256",
        "expected_list_resource_version",
        "expected_receipt_sha256",
        "expected_snapshot_ledger_head_sha256",
        "expected_agents_json",
        "expected_controller_identity_json",
    }:
        raise ValueError("DaemonSet fence verifier query fields differ")
    receipt, receipt_sha256 = verify_live_daemonset_admission_fence(
        str(query["cluster_id"]),
        expected_inventory_sha256=str(query["expected_inventory_sha256"]),
        expected_list_resource_version=str(query["expected_list_resource_version"]),
        expected_agents=json.loads(str(query["expected_agents_json"])),
        expected_controller_identity=json.loads(
            str(query["expected_controller_identity_json"])
        ),
    )
    if (
        receipt_sha256 != query["expected_receipt_sha256"]
        or receipt.get("snapshot_ledger_head_sha256")
        != query["expected_snapshot_ledger_head_sha256"]
        or receipt.get("continuous_enforcement") is not True
    ):
        raise ValueError("live DaemonSet fence differs from provider handoff")
    print(
        json.dumps(
            {
                "authorized": "true",
                "receipt_sha256": receipt_sha256,
                "fence_generation": receipt["fence_generation"],
                "snapshot_ledger_head_sha256": receipt[
                    "snapshot_ledger_head_sha256"
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
