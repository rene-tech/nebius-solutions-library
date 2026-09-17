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
    graph = registry.get("provider_effective_authority_graph_receipt")
    identities = graph.get("principals") if isinstance(graph, dict) else None
    if not isinstance(identities, list) or not identities:
        parser.error("authority registry has no provider-native authority graph")
    principal_ids = {
        str(item.get("id"))
        for item in identities
        if isinstance(item, dict) and item.get("id")
    }
    if len(principal_ids) != len(identities):
        parser.error("authority registry identity principals are incomplete or duplicated")
    receipt = {
        "schema": "fs2-serve.nebius.ai/provider-project-iam-inventory/v2",
        "project_id": registry["authority_project_id"],
        "inventory": _project_inventory(
            args.profile,
            registry["authority_project_id"],
            principal_ids,
        ),
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
