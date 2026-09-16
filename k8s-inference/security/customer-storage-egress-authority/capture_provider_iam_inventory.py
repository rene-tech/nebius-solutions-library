#!/usr/bin/env python3
"""Emit the unsigned body for the external provider IAM inventory receipt."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from verify_authority_ledger import REGISTRY_PATH, safe_root_read, strict_json
from verify_provider_identity import _project_inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    registry = strict_json(safe_root_read(REGISTRY_PATH), "authority registry")
    identities = registry.get("kubernetes_identity_inventory")
    if not isinstance(identities, list) or not identities:
        parser.error("authority registry has no Kubernetes identity inventory")
    principal_ids = {
        str(item.get("provider_principal_id"))
        for item in identities
        if isinstance(item, dict) and item.get("provider_principal_id")
    }
    if len(principal_ids) != len(identities):
        parser.error("authority registry identity principals are incomplete or duplicated")
    receipt = {
        "schema": "fs2-serve.nebius.ai/provider-project-iam-inventory/v1",
        "project_id": registry["authority_project_id"],
        "inventory": _project_inventory(
            args.profile,
            registry["authority_project_id"],
            principal_ids,
        ),
        "cluster_access_principal_ids": sorted(principal_ids),
        "mutating_principal_ids": [registry["authority_group_id"]],
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
