#!/usr/bin/env python3
"""Emit the unsigned body for an independently signed RBAC inventory receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from verify_owner_identity import _kubectl, _rbac_inventory

AUTHORITY_ROOT = Path(__file__).resolve().parents[1] / "customer-storage-egress-authority"
if os.fspath(AUTHORITY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(AUTHORITY_ROOT))

from verify_authority_ledger import (  # noqa: E402
    CONTROLLER_ROLES,
    require_fresh_timestamp,
)
from verify_controller_audit import verify_live_controller_audit  # noqa: E402


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _daemonset_snapshot(
    kubeconfig: Path, context: str
) -> tuple[str, str, list[dict[str, object]]]:
    first = json.loads(
        _kubectl(kubeconfig, context, "get", "daemonsets", "--all-namespaces", "-o", "json")
    )
    resource_version = first.get("metadata", {}).get("resourceVersion")
    items = first.get("items")
    if (
        not isinstance(resource_version, str)
        or not resource_version
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
    ):
        raise ValueError("complete DaemonSet list snapshot is absent")
    second = json.loads(
        _kubectl(
            kubeconfig,
            context,
            "get",
            "daemonsets",
            "--all-namespaces",
            f"--resource-version={resource_version}",
            "--resource-version-match=Exact",
            "-o",
            "json",
        )
    )
    if (
        second.get("metadata", {}).get("resourceVersion") != resource_version
        or second.get("items") != items
    ):
        raise ValueError("DaemonSet inventory changed across the exact double read")
    return resource_version, hashlib.sha256(_canonical(items)).hexdigest(), items


def _audit_authority(
    cluster_id: str,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]], str]:
    receipt, receipt_sha256 = verify_live_controller_audit(cluster_id)
    if (
        set(receipt)
        != {
            "schema",
            "cluster_id",
            "controller_identities",
            "controller_events",
            "daemonset_maintainers",
            "node_health_mutation",
            "observed_at",
            "payload_sha256",
            "signature",
        }
        or receipt.get("schema")
        != "fs2-serve.nebius.ai/kubernetes-controller-audit/v1"
        or receipt.get("cluster_id") != cluster_id
    ):
        raise ValueError("controller audit receipt fields or cluster differ")
    require_fresh_timestamp(receipt.get("observed_at"), "controller audit receipt")
    identities = receipt.get("controller_identities")
    events = receipt.get("controller_events")
    maintainers = receipt.get("daemonset_maintainers")
    if (
        not isinstance(identities, dict)
        or set(identities) != CONTROLLER_ROLES
        or not isinstance(events, dict)
        or set(events) != CONTROLLER_ROLES
        or not isinstance(maintainers, dict)
    ):
        raise ValueError("controller audit identity closure is incomplete")
    expected_resources = {
        "deployment": ("apps", "replicasets", "", {"create"}),
        "replicaset": ("", "pods", "", {"create"}),
        "daemonset": ("", "pods", "", {"create"}),
        "scheduler": ("", "pods", "binding", {"create"}),
        "node_health": ("", "nodes", "", {"update", "patch"}),
    }
    for role, identity in identities.items():
        event = events.get(role)
        if not isinstance(identity, dict) or not isinstance(event, dict):
            raise ValueError(f"{role} controller audit evidence is malformed")
        user_info = event.get("user_info")
        object_ref = event.get("object_ref")
        if (
            set(event)
            != {
                "audit_id",
                "stage",
                "stage_timestamp",
                "verb",
                "object_ref",
                "user_info",
                "response_code",
            }
            or event.get("stage") != "ResponseComplete"
            or event.get("response_code") not in {200, 201}
            or not isinstance(event.get("audit_id"), str)
            or not event["audit_id"]
            or not isinstance(object_ref, dict)
            or set(object_ref)
            != {"api_group", "resource", "subresource", "namespace", "name", "uid"}
            or object_ref.get("api_group") != expected_resources[role][0]
            or object_ref.get("resource") != expected_resources[role][1]
            or object_ref.get("subresource") != expected_resources[role][2]
            or event.get("verb") not in expected_resources[role][3]
            or not isinstance(object_ref.get("name"), str)
            or not object_ref["name"]
            or not isinstance(object_ref.get("uid"), str)
            or not object_ref["uid"]
            or not isinstance(user_info, dict)
            or user_info
            != {
                "username": identity.get("username"),
                "uid": identity.get("uid"),
                "groups": identity.get("groups"),
            }
            or identity.get("audit_evidence_sha256")
            != hashlib.sha256(_canonical(event)).hexdigest()
        ):
            raise ValueError(f"{role} controller identity lacks an authenticated audit event")
        require_fresh_timestamp(event.get("stage_timestamp"), f"{role} controller audit event")
    for key, maintainer in maintainers.items():
        if (
            not isinstance(key, str)
            or "/" not in key
            or not isinstance(maintainer, dict)
            or set(maintainer) != {"identity", "event", "event_sha256"}
            or not isinstance(maintainer.get("identity"), dict)
            or not isinstance(maintainer.get("event"), dict)
            or maintainer.get("event_sha256")
            != hashlib.sha256(_canonical(maintainer["event"])).hexdigest()
        ):
            raise ValueError("DaemonSet maintainer audit evidence is malformed")
        namespace, name = key.split("/", 1)
        event = maintainer["event"]
        if (
            set(event)
            != {
                "audit_id",
                "stage",
                "stage_timestamp",
                "verb",
                "object_ref",
                "user_info",
                "response_code",
            }
            or event.get("stage") != "ResponseComplete"
            or event.get("verb") not in {"update", "patch"}
            or event.get("response_code") not in {200, 201}
            or event.get("object_ref")
            != {
                "api_group": "apps",
                "resource": "daemonsets",
                "subresource": "",
                "namespace": namespace,
                "name": name,
                "uid": event.get("object_ref", {}).get("uid"),
            }
            or not isinstance(event.get("object_ref", {}).get("uid"), str)
            or not event["object_ref"]["uid"]
            or event.get("user_info")
            != {
                "username": maintainer["identity"].get("username"),
                "uid": maintainer["identity"].get("uid"),
                "groups": maintainer["identity"].get("groups"),
            }
        ):
            raise ValueError("DaemonSet maintainer was not authenticated by exact audit evidence")
        require_fresh_timestamp(event.get("stage_timestamp"), f"{key} maintainer audit event")
    return identities, maintainers, receipt_sha256


def _blanket_tolerating_agents(
    items: list[dict[str, object]],
    maintainers: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    agents: dict[str, dict[str, object]] = {}
    for daemonset in items:
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
        maintainer = maintainers.get(key)
        if not isinstance(maintainer, dict):
            raise ValueError("blanket-tolerating DaemonSet has no audit-proven maintainer")
        created = metadata.get("creationTimestamp")
        try:
            created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("blanket-tolerating DaemonSet creation time is invalid") from exc
        if created_at.tzinfo is None:
            raise ValueError("blanket-tolerating DaemonSet creation time has no timezone")
        spec_sha256 = hashlib.sha256(_canonical(spec)).hexdigest()
        snapshot_body = {
            "namespace": namespace,
            "name": name,
            "uid": uid,
            "daemonset_spec_sha256": spec_sha256,
            "pod_template_sha256": hashlib.sha256(
                _canonical(spec.get("template"))
            ).hexdigest(),
            "owner_identity_sha256": hashlib.sha256(
                _canonical(maintainer["identity"])
            ).hexdigest(),
        }
        snapshot_sha256 = hashlib.sha256(_canonical(snapshot_body)).hexdigest()
        snapshot_generation = (
            f"s{created_at.astimezone(UTC).strftime('%Y%m%d%H%M%S')}"
            f"-{snapshot_sha256[:12]}"
        )
        agents[key] = {
            "namespace": namespace,
            "name": name,
            "uid": uid,
            "snapshot_generation": snapshot_generation,
            "snapshot_sha256": snapshot_sha256,
            "daemonset_spec": spec,
            "daemonset_spec_sha256": spec_sha256,
            "maintenance_identity": maintainer["identity"],
            "maintenance_audit_sha256": maintainer["event_sha256"],
        }
    if not agents or set(agents) != set(maintainers):
        raise ValueError("audit and complete live blanket-agent inventories differ")
    return dict(sorted(agents.items()))


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
    controller_identities, maintainers, controller_audit_receipt_sha256 = (
        _audit_authority(args.cluster_id)
    )
    list_resource_version, daemonset_inventory_sha256, daemonsets = (
        _daemonset_snapshot(args.kubeconfig, args.context)
    )
    receipt = {
        "schema": "fs2-serve.nebius.ai/kubernetes-rbac-inventory/v6",
        "cluster_id": args.cluster_id,
        "inventory_sha256": inventory_sha256,
        "subjects": subjects,
        "effective_authority": effective_authority,
        "blanket_tolerating_agents": _blanket_tolerating_agents(
            daemonsets, maintainers
        ),
        "daemonset_list_resource_version": list_resource_version,
        "daemonset_inventory_sha256": daemonset_inventory_sha256,
        "controller_identities": controller_identities,
        "controller_audit_receipt_sha256": controller_audit_receipt_sha256,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
