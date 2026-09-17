#!/usr/bin/env python3
"""Emit the unsigned body for an independently signed RBAC inventory receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from verify_owner_identity import _kubectl, _rbac_inventory


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _blanket_tolerating_agents(
    kubeconfig: Path, context: str
) -> dict[str, dict[str, object]]:
    response = json.loads(
        _kubectl(kubeconfig, context, "get", "daemonsets", "--all-namespaces", "-o", "json")
    )
    agents: dict[str, dict[str, object]] = {}
    for daemonset in response.get("items", []):
        metadata = daemonset.get("metadata", {})
        spec = daemonset.get("spec", {})
        pod_spec = spec.get("template", {}).get("spec", {})
        tolerations = pod_spec.get("tolerations", [])
        if not any(
            isinstance(toleration, dict)
            and toleration.get("key") in {None, ""}
            and toleration.get("operator") == "Exists"
            and toleration.get("effect") in {None, "", "NoSchedule"}
            for toleration in tolerations
        ):
            continue
        namespace = metadata.get("namespace")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not all(isinstance(value, str) and value for value in (namespace, name, uid)):
            raise ValueError("blanket-tolerating DaemonSet identity is incomplete")
        key = f"{namespace}/{name}"
        agents[key] = {
            "namespace": namespace,
            "name": name,
            "uid": uid,
            "daemonset_spec": spec,
            "daemonset_spec_sha256": hashlib.sha256(_canonical(spec)).hexdigest(),
        }
    return dict(sorted(agents.items()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument(
        "--controller-identities-json",
        required=True,
        help="Audit-derived controller userInfo map to bind into the signed receipt",
    )
    args = parser.parse_args()
    if not args.cluster_id:
        parser.error("--cluster-id is required")
    inventory_sha256, subjects, effective_authority = _rbac_inventory(
        args.kubeconfig, args.context
    )
    controller_identities = json.loads(args.controller_identities_json)
    if not isinstance(controller_identities, dict) or set(controller_identities) != {
        "deployment",
        "replicaset",
        "daemonset",
        "scheduler",
    }:
        parser.error("--controller-identities-json must contain all four audited roles")
    receipt = {
        "schema": "fs2-serve.nebius.ai/kubernetes-rbac-inventory/v4",
        "cluster_id": args.cluster_id,
        "inventory_sha256": inventory_sha256,
        "subjects": subjects,
        "effective_authority": effective_authority,
        "blanket_tolerating_agents": _blanket_tolerating_agents(
            args.kubeconfig, args.context
        ),
        "controller_identities": controller_identities,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
