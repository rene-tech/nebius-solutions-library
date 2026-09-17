#!/usr/bin/env python3
"""Verify the complete live DaemonSet snapshot before and after admission."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from capture_kubernetes_rbac_inventory import (
    _blanket_tolerating_agents,
    _daemonset_snapshot,
)


def main() -> int:
    query = json.load(sys.stdin)
    if not isinstance(query, dict) or set(query) != {
        "kubeconfig_path",
        "kube_context",
        "expected_agents_json",
        "expected_inventory_sha256",
    }:
        raise ValueError("live DaemonSet verifier query fields differ")
    expected = json.loads(query["expected_agents_json"])
    if not isinstance(expected, dict) or not expected:
        raise ValueError("signed blanket-agent inventory is absent")
    maintainers = {
        key: {
            "identity": value["maintenance_identity"],
            "event_sha256": value["maintenance_audit_sha256"],
        }
        for key, value in expected.items()
    }
    resource_version, inventory_sha256, daemonsets = _daemonset_snapshot(
        Path(query["kubeconfig_path"]), query["kube_context"]
    )
    observed = _blanket_tolerating_agents(daemonsets, maintainers)
    if (
        inventory_sha256 != query["expected_inventory_sha256"]
        or observed != expected
    ):
        raise ValueError("complete live DaemonSet inventory differs from signed custody")
    print(
        json.dumps(
            {
                "authorized": "true",
                "list_resource_version": resource_version,
                "inventory_sha256": inventory_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
