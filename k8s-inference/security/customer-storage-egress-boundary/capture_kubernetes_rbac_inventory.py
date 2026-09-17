#!/usr/bin/env python3
"""Emit the unsigned body for an independently signed RBAC inventory receipt."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from verify_owner_identity import _rbac_inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster-id", required=True)
    args = parser.parse_args()
    if not args.cluster_id:
        parser.error("--cluster-id is required")
    inventory_sha256, subjects, effective_authority = _rbac_inventory(
        args.kubeconfig, args.context
    )
    receipt = {
        "schema": "fs2-serve.nebius.ai/kubernetes-rbac-inventory/v3",
        "cluster_id": args.cluster_id,
        "inventory_sha256": inventory_sha256,
        "subjects": subjects,
        "effective_authority": effective_authority,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
