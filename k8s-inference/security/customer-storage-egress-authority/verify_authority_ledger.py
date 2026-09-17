#!/usr/bin/env python3
"""Verify the root-owned approval and signed append-only provider boundary."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SECURITY_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(SECURITY_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SECURITY_ROOT))

from rbac_authority import CONTROLLER_ROLES, verify_subject_inventory  # noqa: E402
from verify_controller_audit import verify_live_controller_audit  # noqa: E402
from verify_daemonset_admission_fence import (  # noqa: E402
    _source_sha256,
    verify_live_daemonset_admission_fence,
)
from storage_reconciler_cutover_runtime import (  # noqa: E402
    load_verified_state as load_cutover_state,
)

REGISTRY_PATH = Path("/etc/fs2-security-ro/authority/customer-storage-egress-authority.json")
PRIOR_HEAD_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/customer-storage-egress-prior-head.json"
)
REGISTRY_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-registry/v9"
PRIOR_HEAD_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-prior-head/v5"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-ledger/v9"
MAX_BYTES = 1024 * 1024
NEBIUS_TERRAFORM_PROVIDER_VERSION = "0.5.232"
LANE_CUSTODY_ADAPTER = Path("/usr/libexec/fs2-security/lane-provisioning-custody")
ACCEPTED_SAI10_COMMIT = "057386a3e0c616d79735adb43a97c19c48046608"
ACCEPTED_SAI10_TREE = "a244a4264b1ad5ad782848eed04d9986524921b4"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LEGACY_GENERATION_FIELDS = {
    "generation",
    "predecessor_sha256",
    "contract_sha256",
    "predecessor_compatibility_sha256",
    "boundary_policy_sha256",
    "workload_policy_sha256",
    "release_values_sha256",
    "provider_iam_receipt_sha256",
    "provider_authority_graph_receipt_sha256",
    "kubernetes_rbac_receipt_sha256",
    "provider_state_custody_sha256",
    "boundary_state_custody_sha256",
    "prior_live_custody_sha256",
    "accepted_sai10_commit",
    "accepted_sai10_tree",
    "accepted_sai10_review_sha256",
    "cluster_id",
    "network_id",
    "subnet_id",
    "node_service_account_id",
    "kubernetes_version",
    "nebius_terraform_provider_version",
    "provider_api_cidrs",
    "kubernetes_api_cidrs",
    "bootstrap_https_cidrs",
    "private_cidrs",
    "platform",
    "preset",
    "boot_disk_type",
    "boot_disk_gib",
}
PROTECTED_LANE_V2_GENERATION_FIELDS = LEGACY_GENERATION_FIELDS | {
    "lane_id",
    "scheduling_key",
    "protected_observers",
    "protected_observer_inventory_sha256",
    "protected_node_names",
    "protected_node_inventory_sha256",
    "min_node_count",
    "max_node_count",
}
PROTECTED_LANE_V3_GENERATION_FIELDS = PROTECTED_LANE_V2_GENERATION_FIELDS | {
    "protected_node_scheduling_labels",
    "protected_node_scheduling_labels_sha256",
}
PROTECTED_LANE_V4_GENERATION_FIELDS = PROTECTED_LANE_V3_GENERATION_FIELDS | {
    "provisioning_generation",
    "provisioning_receipt_sha256",
    "security_group_id",
    "node_group_id",
    "protected_node_attestations",
    "protected_node_attestation_sha256",
}
PROTECTED_LANE_V5_GENERATION_FIELDS = PROTECTED_LANE_V4_GENERATION_FIELDS | {
    "controller_audit_receipt_sha256",
    "daemonset_inventory_sha256",
    "daemonset_list_resource_version",
    "node_health_mutation",
}
GENERATION_FIELDS = PROTECTED_LANE_V5_GENERATION_FIELDS | {
    "daemonset_admission_fence_receipt_sha256",
    "daemonset_snapshot_ledger_head_sha256",
    "node_lifecycle_mode",
    "reconciler_cutover_receipt_sha256",
    "reconciler_activation_endpoint",
    "reconciler_activation_public_key_config_map_name",
    "reconciler_activation_ca_config_map_name",
    "reconciler_activation_minimum_epoch",
}
PROVISIONING_V2_FIELDS = {
    "lane_id",
    "scheduling_key",
    "cluster_id",
    "network_id",
    "subnet_id",
    "node_service_account_id",
    "kubernetes_version",
    "nebius_terraform_provider_version",
    "provider_api_cidrs",
    "kubernetes_api_cidrs",
    "bootstrap_https_cidrs",
    "private_cidrs",
    "platform",
    "preset",
    "boot_disk_type",
    "boot_disk_gib",
    "min_node_count",
    "max_node_count",
}
PROVISIONING_FIELDS = PROVISIONING_V2_FIELDS | {"node_lifecycle_mode"}
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
LEGACY_PROTECTED_OBSERVER_ROLES = {
    "gpu-allocation-observer",
    "otel-node",
    "filesystem-csi",
    "prometheus-node-exporter",
    "retained-otel-node",
}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_root_read_with_identity(path: Path) -> tuple[bytes, tuple[int, int]]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("authority registry path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        ):
            raise ValueError(
                "authority input must be a bounded root-owned private file on a read-only filesystem"
            )
        payload = b""
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("authority registry changed during its descriptor-bound read")
        return payload, (before.st_dev, before.st_ino)
    finally:
        os.close(descriptor)


def safe_root_read(path: Path) -> bytes:
    return safe_root_read_with_identity(path)[0]


def strict_json(payload: bytes | str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains a duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def host_routes(values: object, label: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ValueError(f"{label} must be a bounded non-empty list")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{label} entries must be strings")
        network = ipaddress.ip_network(raw, strict=True)
        if network.prefixlen != network.max_prefixlen:
            raise ValueError(f"{label} accepts only exact host routes")
        normalized.append(network.with_prefixlen)
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        raise ValueError(f"{label} must be unique and sorted")
    return normalized


def private_routes(values: object) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError("private CIDRs must be a bounded non-empty list")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("private CIDRs must be strings")
        network = ipaddress.ip_network(raw, strict=True)
        if not network.is_private:
            raise ValueError("private CIDRs must be non-public networks")
        normalized.append(network.with_prefixlen)
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        raise ValueError("private CIDRs must be unique and sorted")
    return normalized


def digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is not a SHA-256 digest")
    return value


def protected_observers(value: object, lane_id: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("complete signed protected-agent inventory is required")
    if set(value) == LEGACY_PROTECTED_OBSERVER_ROLES and all(
        isinstance(observer, dict)
        and set(observer)
        == {
            "namespace",
            "name",
            "uid",
            "owner_username",
            "daemonset_spec",
            "daemonset_spec_sha256",
        }
        for observer in value.values()
    ):
        for role, observer in value.items():
            if (
                not UID_RE.fullmatch(str(observer.get("uid", "")))
                or hashlib.sha256(canonical(observer.get("daemonset_spec"))).hexdigest()
                != observer.get("daemonset_spec_sha256")
            ):
                raise ValueError(f"{role} retained protected-observer custody differs")
        return value
    scheduling_key = f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
    seen_names: set[tuple[str, str]] = set()
    seen_uids: set[str] = set()
    for role, observer in value.items():
        base_fields = {
            "class",
            "namespace",
            "name",
            "uid",
            "owner_identity",
            "daemonset_spec",
            "daemonset_spec_sha256",
        }
        critical_fields = base_fields | {
            "maintenance_audit_sha256",
            "snapshot_generation",
            "snapshot_sha256",
        }
        if not isinstance(observer, dict) or frozenset(observer) not in {
            frozenset(base_fields),
            frozenset(critical_fields),
        }:
            raise ValueError(f"{role} protected-observer fields differ")
        observer_class = observer.get("class")
        namespace = observer.get("namespace")
        name = observer.get("name")
        uid = observer.get("uid")
        owner = observer.get("owner_identity")
        spec = observer.get("daemonset_spec")
        owner_match = (
            re.fullmatch(
                r"system:serviceaccount:([^:]+):([^:]+)",
                str(owner.get("username", "")),
            )
            if isinstance(owner, dict)
            else None
        )
        if (
            not isinstance(namespace, str)
            or not namespace
            or not isinstance(name, str)
            or not name
            or not isinstance(uid, str)
            or not UID_RE.fullmatch(uid)
            or observer_class not in {"lane", "critical-blanket-agent"}
            or not isinstance(owner, dict)
            or set(owner) != {"username", "uid", "groups"}
            or not isinstance(owner.get("username"), str)
            or not owner["username"]
            or not isinstance(owner.get("uid"), str)
            or not UID_RE.fullmatch(owner["uid"])
            or not isinstance(owner.get("groups"), list)
            or owner_match is None
            or owner["groups"]
            != [
                "system:authenticated",
                "system:serviceaccounts",
                f"system:serviceaccounts:{owner_match.group(1)}",
            ]
            or not isinstance(spec, dict)
            or hashlib.sha256(canonical(spec)).hexdigest()
            != observer.get("daemonset_spec_sha256")
            or (
                "maintenance_audit_sha256" in observer
                and not re.fullmatch(
                    r"[a-f0-9]{64}", str(observer["maintenance_audit_sha256"])
                )
            )
            or (
                observer_class == "critical-blanket-agent"
                and (
                    "maintenance_audit_sha256" not in observer
                    or not re.fullmatch(
                        r"s[0-9]{14}-[a-f0-9]{12}",
                        str(observer.get("snapshot_generation", "")),
                    )
                    or not re.fullmatch(
                        r"[a-f0-9]{64}", str(observer.get("snapshot_sha256", ""))
                    )
                )
            )
            or (
                observer_class != "critical-blanket-agent"
                and frozenset(observer) != frozenset(base_fields)
            )
        ):
            raise ValueError(f"{role} protected-observer identity or spec is invalid")
        if observer_class == "lane" and namespace != "kube-system":
            raise ValueError(f"{role} additive observer identity is not lane-bound")
        if (namespace, name) in seen_names or uid in seen_uids:
            raise ValueError("protected-observer names and UIDs must be unique")
        seen_names.add((namespace, name))
        seen_uids.add(uid)
        template = spec.get("template")
        if not isinstance(template, dict):
            raise ValueError(f"{role} protected-observer template is absent")
        metadata = template.get("metadata")
        pod_spec = template.get("spec")
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("labels"), dict)
            or spec.get("selector") != {"matchLabels": metadata["labels"]}
            or not isinstance(
                metadata["labels"].get("app.kubernetes.io/component"), str
            )
            or not metadata["labels"]["app.kubernetes.io/component"]
            or not isinstance(pod_spec, dict)
            or not isinstance(pod_spec.get("containers"), list)
            or not pod_spec["containers"]
            or any(not isinstance(container, dict) for container in pod_spec["containers"])
            or pod_spec.get("nodeName") not in {None, ""}
        ):
            raise ValueError(f"{role} protected-observer scheduling contract differs")
        if observer_class == "lane":
            if (
                not isinstance(pod_spec.get("serviceAccountName"), str)
                or not pod_spec["serviceAccountName"]
                or pod_spec.get("automountServiceAccountToken") is not False
                or any(
                    not re.fullmatch(
                        r"[^@]+@sha256:[a-f0-9]{64}",
                        str(container.get("image", "")),
                    )
                    for container in pod_spec["containers"]
                )
                or
                metadata["labels"].get("fs2.nebius.ai/protected-lane-id") != lane_id
                or pod_spec.get("nodeSelector") != {scheduling_key: lane_id}
                or pod_spec.get("tolerations")
                != [
                    {
                        "key": scheduling_key,
                        "operator": "Equal",
                        "value": lane_id,
                        "effect": "NoSchedule",
                    }
                ]
            ):
                raise ValueError(f"{role} additive observer scheduling differs")
        else:
            selector = pod_spec.get("nodeSelector", {})
            if (
                metadata["labels"].get("fs2.nebius.ai/protected-lane-id") is not None
                or not isinstance(selector, dict)
                or scheduling_key in selector
                or not any(
                    isinstance(toleration, dict)
                    and toleration.get("key") in {None, ""}
                    and toleration.get("operator") == "Exists"
                    and toleration.get("effect") in {None, "", "NoSchedule"}
                    for toleration in pod_spec.get("tolerations", [])
                )
            ):
                raise ValueError(f"{role} retained node-agent scheduling differs")
    return value


def protected_node_names(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != 1
        or any(not isinstance(node_name, str) for node_name in value)
        or value != sorted(set(value))
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", value[0])
    ):
        raise ValueError("exact activated protected-node inventory is invalid")
    return value


def protected_node_scheduling_labels(
    value: object,
    *,
    node_names: list[str],
    scheduling_key: str,
    lane_id: str,
) -> dict[str, dict[str, str]]:
    if (
        not isinstance(value, dict)
        or set(value) != set(node_names)
        or any(
            not isinstance(labels, dict)
            or not labels
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(label_value, str)
                for key, label_value in labels.items()
            )
            for labels in value.values()
        )
        or value[node_names[0]].get(scheduling_key) != lane_id
    ):
        raise ValueError("complete protected-node scheduling-label projection is invalid")
    return value


def protected_node_attestations(
    value: object,
    *,
    node_names: list[str],
    scheduling_labels: dict[str, dict[str, str]],
    scheduling_key: str,
    lane_id: str,
    provisioning_receipt: dict[str, Any] | None = None,
    provisioning_receipt_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != set(node_names):
        raise ValueError("exact protected-node attestations are required")
    for node_name, attestation in value.items():
        expected_fields = {"name", "uid", "resource_version", "labels", "taints"}
        if provisioning_receipt is not None:
            expected_fields |= {
                "provider_id",
                "node_group_id",
                "provisioning_receipt_sha256",
                "observed_at",
            }
        provider_inventory = (
            provisioning_receipt.get("provider_inventory")
            if isinstance(provisioning_receipt, dict)
            else None
        )
        node_group = (
            provider_inventory.get("node_group")
            if isinstance(provider_inventory, dict)
            else None
        )
        members = node_group.get("members", []) if isinstance(node_group, dict) else []
        if (
            not isinstance(attestation, dict)
            or set(attestation) != expected_fields
            or attestation.get("name") != node_name
            or not UID_RE.fullmatch(str(attestation.get("uid", "")))
            or not isinstance(attestation.get("resource_version"), str)
            or not attestation["resource_version"]
            or attestation.get("labels") != scheduling_labels[node_name]
            or not isinstance(attestation.get("taints"), list)
            or {
                "key": scheduling_key,
                "value": lane_id,
                "effect": "NoSchedule",
            }
            not in attestation["taints"]
            or (
                provisioning_receipt is not None
                and (
                    attestation.get("node_group_id")
                    != provisioning_receipt.get("node_group_id")
                    or attestation.get("provisioning_receipt_sha256")
                    != provisioning_receipt_sha256
                    or not isinstance(attestation.get("provider_id"), str)
                    or not attestation["provider_id"]
                    or len(
                        [
                            member
                            for member in members
                            if member.get("provider_id") == attestation["provider_id"]
                            and member.get("node_group_id")
                            == attestation["node_group_id"]
                        ]
                    )
                    != 1
                )
            )
        ):
            raise ValueError("protected-node identity, labels, or taints differ")
        if provisioning_receipt is not None:
            require_fresh_timestamp(
                attestation.get("observed_at"), "protected Node attestation"
            )
    return value


def provisioning_generation(entry: dict[str, Any]) -> str:
    fields = (
        PROVISIONING_FIELDS
        if "node_lifecycle_mode" in entry
        else PROVISIONING_V2_FIELDS
    )
    payload = {field: entry[field] for field in sorted(fields)}
    observed = entry.get("provisioning_generation")
    payload_sha256 = hashlib.sha256(canonical(payload)).hexdigest()
    if (
        not isinstance(observed, str)
        or not re.fullmatch(r"p[0-9]{14}-[a-f0-9]{12}", observed)
        or observed[-12:] != payload_sha256[:12]
    ):
        raise ValueError("stable provider provisioning generation is not content-bound")
    return observed


def expected_lane_managed_addresses(provisioning_ids: set[str]) -> list[str]:
    resources = (
        "terraform_data.signed_provisioning",
        "nebius_vpc_v1_security_group.lane",
        "nebius_vpc_v1_security_rule.private_ingress",
        "nebius_vpc_v1_security_rule.dns_egress",
        "nebius_vpc_v1_security_rule.database_egress",
        "nebius_vpc_v1_security_rule.provider_egress",
        "nebius_mk8s_v1_node_group.lane",
    )
    return sorted(
        f"{resource}[{json.dumps(generation)}]"
        for generation in sorted(provisioning_ids)
        for resource in resources
    )


def expected_lane_labels(entry: dict[str, Any], provisioning_id: str) -> dict[str, str]:
    return {
        "managed-by": "fs2-lane-security-owner",
        "security-boundary": "customer-storage-egress",
        "provisioning-generation": provisioning_id,
        "lane-id": entry["lane_id"],
    }


def expected_lane_rules(
    entry: dict[str, Any], provisioning_id: str
) -> list[dict[str, Any]]:
    labels = expected_lane_labels(entry, provisioning_id)
    provider_cidrs = sorted(
        set(
            entry["provider_api_cidrs"]
            + entry["kubernetes_api_cidrs"]
            + entry["bootstrap_https_cidrs"]
        )
    )
    common = {"access": "ALLOW", "type": "STATEFUL", "priority": 100}
    rules = [
        {
            **common,
            "name": f"fs2-storage-private-{provisioning_id}",
            "labels": {**labels, "purpose": "private-ingress"},
            "protocol": "ANY",
            "direction": "INGRESS",
            "source_cidrs": entry["private_cidrs"],
            "destination_cidrs": [],
            "destination_ports": [],
        },
        {
            **common,
            "name": f"fs2-storage-dns-{provisioning_id}",
            "labels": {**labels, "purpose": "dns-egress"},
            "protocol": "ANY",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": entry["private_cidrs"],
            "destination_ports": [53],
        },
        {
            **common,
            "name": f"fs2-storage-db-{provisioning_id}",
            "labels": {**labels, "purpose": "database-egress"},
            "protocol": "TCP",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": entry["private_cidrs"],
            "destination_ports": [5432],
        },
        {
            **common,
            "name": f"fs2-storage-provider-{provisioning_id}",
            "labels": {**labels, "purpose": "provider-egress"},
            "protocol": "TCP",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": provider_cidrs,
            "destination_ports": [443],
        },
    ]
    return sorted(rules, key=lambda item: item["name"])


def verify_lane_network_contract(
    receipt: dict[str, Any], entry: dict[str, Any], provisioning_ids: set[str]
) -> None:
    provisioning_id = str(receipt["provisioning_generation"])
    backend = receipt.get("backend_custody")
    provider = receipt.get("provider_inventory")
    security_group = provider.get("security_group") if isinstance(provider, dict) else None
    node_group = provider.get("node_group") if isinstance(provider, dict) else None
    if (
        not isinstance(backend, dict)
        or not isinstance(security_group, dict)
        or not isinstance(node_group, dict)
    ):
        raise ValueError("lane semantic provider/backend custody is absent")
    expected_addresses = expected_lane_managed_addresses(provisioning_ids)
    labels = expected_lane_labels(entry, provisioning_id)
    expected_rules = expected_lane_rules(entry, provisioning_id)
    live_rules = security_group.get("rules")
    normalized_rules: list[dict[str, Any]] = []
    rule_ids: list[str] = []
    if isinstance(live_rules, list):
        for rule in live_rules:
            if (
                not isinstance(rule, dict)
                or set(rule)
                != {
                    "id",
                    "name",
                    "labels",
                    "access",
                    "protocol",
                    "type",
                    "priority",
                    "direction",
                    "source_cidrs",
                    "destination_cidrs",
                    "destination_ports",
                }
                or not isinstance(rule.get("id"), str)
                or not rule["id"]
            ):
                raise ValueError("lane security-group rule shape differs")
            rule_ids.append(rule["id"])
            normalized_rules.append(
                {key: value for key, value in rule.items() if key != "id"}
            )
    normalized_rules.sort(key=lambda item: item["name"])
    network_contract = {
        "security_group_labels": labels,
        "security_group_rules": expected_rules,
        "node_group_labels": labels,
        "node_group_strategy": {
            "max_surge": 0,
            "max_unavailable": 0,
            "drain_timeout": "30m",
        },
    }
    if (
        backend.get("managed_addresses") != expected_addresses
        or receipt.get("expected_managed_addresses_sha256")
        != hashlib.sha256(canonical(expected_addresses)).hexdigest()
        or security_group.get("labels") != labels
        or len(rule_ids) != len(set(rule_ids))
        or normalized_rules != expected_rules
        or node_group.get("labels") != labels
        or node_group.get("strategy") != network_contract["node_group_strategy"]
        or node_group.get("template_labels")
        != {
            entry["scheduling_key"]: entry["lane_id"],
            "fs2.nebius.ai/provisioning-generation": provisioning_id,
        }
        or node_group.get("template_taints")
        != [
            {
                "key": entry["scheduling_key"],
                "value": entry["lane_id"],
                "effect": "NO_SCHEDULE",
            }
        ]
        or node_group.get("security_group_ids") != [security_group.get("id")]
        or len(node_group.get("members", [])) != 1
        or receipt.get("network_contract_sha256")
        != hashlib.sha256(canonical(network_contract)).hexdigest()
        or receipt.get("node_lifecycle_mode")
        != "PARALLEL_GENERATIONAL_SINGLETON_CUTOVER_RETAIN_PREDECESSOR"
    ):
        raise ValueError("lane SG, exact egress, state, or singleton contract differs")


def require_fresh_timestamp(value: object, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp has no timezone")
    parsed = parsed.astimezone(UTC)
    now = datetime.now(UTC)
    if parsed > now or now - parsed > timedelta(hours=24):
        raise ValueError(f"{label} receipt is future-dated or stale")


def verify_live_lane_custody(
    receipt: dict[str, Any], expected_adapter_sha256: str
) -> None:
    """Re-read remote state and provider resources through the pinned adapter."""

    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in LANE_CUSTODY_ADAPTER.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            LANE_CUSTODY_ADAPTER.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        adapter = os.read(descriptor, before.st_size + 1)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
            or len(adapter) != before.st_size
            or hashlib.sha256(adapter).hexdigest() != expected_adapter_sha256
        ):
            raise ValueError("lane custody adapter differs from pinned approval")
        provider = receipt.get("provider_inventory")
        node_group = provider.get("node_group") if isinstance(provider, dict) else None
        security_group = (
            provider.get("security_group") if isinstance(provider, dict) else None
        )
        labels = node_group.get("template_labels") if isinstance(node_group, dict) else None
        lane_labels = (
            [
                (key, value)
                for key, value in labels.items()
                if key.startswith("workload.fs2.nebius/customer-storage-egress-")
                and isinstance(value, str)
                and value.startswith("l")
            ]
            if isinstance(labels, dict)
            else []
        )
        if len(lane_labels) != 1 or not isinstance(security_group, dict):
            raise ValueError("signed live lane identity is ambiguous")
        request = {
            "schema": "fs2-serve.nebius.ai/protected-lane-provisioning-custody-request/v2",
            "manifest_sha256": receipt["manifest_sha256"],
            "provisioning_generation": receipt["provisioning_generation"],
            "authority_project_id": receipt["authority_project_id"],
            "cluster_id": receipt["cluster_id"],
            "network_id": security_group["network_id"],
            "lane_id": lane_labels[0][1],
            "scheduling_key": lane_labels[0][0],
        }
        result = subprocess.run(
            [f"/proc/self/fd/{descriptor}"],
            input=json.dumps(request, sort_keys=True, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            pass_fds=(descriptor,),
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        result.returncode != 0
        or not result.stdout
        or len(result.stdout.encode()) > 8 * MAX_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("lane custody adapter failed or changed during live re-read")
    live = strict_json(result.stdout, "live lane custody")
    if (
        set(live)
        != {"schema", "observed_at", "backend_custody", "provider_inventory"}
        or live.get("schema")
        != "fs2-serve.nebius.ai/protected-lane-provisioning-custody/v2"
        or live.get("backend_custody") != receipt.get("backend_custody")
        or live.get("provider_inventory") != receipt.get("provider_inventory")
    ):
        raise ValueError("live provider/backend lane custody differs from signed receipt")
    require_fresh_timestamp(live.get("observed_at"), "live lane custody")


def git_is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode not in {0, 1}:
        raise ValueError("accepted Git ancestry could not be verified")
    return result.returncode == 0


def verify_signed_object(
    value: dict[str, Any],
    *,
    public_key_pem: str,
    digest_field: str = "payload_sha256",
) -> str:
    body = {key: item for key, item in value.items() if key not in {digest_field, "signature"}}
    payload = canonical(body)
    payload_digest = hashlib.sha256(payload).hexdigest()
    if value.get(digest_field) != payload_digest:
        raise ValueError("signed authority object payload digest differs")
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("authority key must be Ed25519")
        signature = base64.b64decode(value["signature"], validate=True)
        key.verify(signature, payload)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ValueError("signed authority object signature is invalid") from exc
    return payload_digest


def verify(manifest_json: str) -> dict[str, str]:
    registry_payload, registry_identity = safe_root_read_with_identity(REGISTRY_PATH)
    prior_payload, prior_identity = safe_root_read_with_identity(PRIOR_HEAD_PATH)
    if registry_identity[0] == prior_identity[0]:
        raise ValueError("authority approval and prior head must use distinct read-only filesystems")
    registry = strict_json(registry_payload, "authority registry")
    prior_head = strict_json(prior_payload, "authority prior head")
    expected_registry_fields = {
        "schema",
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "authority_access_permits",
        "authority_auth_public_keys",
        "workloads_service_account_id",
        "kubernetes_identity_inventory",
        "kubernetes_service_account_inventory",
        "kubernetes_system_subject_inventory",
        "kubernetes_controller_identities",
        "kubernetes_controller_audit_receipt",
        "kubernetes_daemonset_admission_fence_receipt",
        "lane_provisioning_receipts",
        "lane_provisioning_custody_adapter_sha256",
        "kubernetes_rbac_inventory_receipt",
        "provider_project_iam_inventory_receipt",
        "provider_effective_authority_graph_receipt",
        "provider_authority_adapter_sha256",
        "accepted_custody",
        "approved_manifest_sha256",
        "manifest_public_key_pem",
        "checkpoint_public_key_pem",
    }
    if set(registry) != expected_registry_fields or registry.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("authority registry fields or schema differ")
    for field in (
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "workloads_service_account_id",
        "approved_manifest_sha256",
        "manifest_public_key_pem",
        "checkpoint_public_key_pem",
        "provider_authority_adapter_sha256",
    ):
        if not isinstance(registry[field], str) or not registry[field]:
            raise ValueError("authority registry identity is incomplete")
    digest(registry["approved_manifest_sha256"], "approved manifest digest")
    digest(registry["provider_authority_adapter_sha256"], "provider authority adapter")
    digest(
        registry["lane_provisioning_custody_adapter_sha256"],
        "lane provisioning custody adapter",
    )
    for field, expected_fields in (
        ("authority_access_permits", {"id", "role", "resource_id"}),
        ("authority_auth_public_keys", {"id", "expires_at"}),
    ):
        values = registry[field]
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(item, dict)
                or set(item) != expected_fields
                or any(not isinstance(value, str) or not value for value in item.values())
                for item in values
            )
        ):
            raise ValueError(f"authority registry {field} is incomplete")
        if len({item["id"] for item in values}) != len(values):
            raise ValueError(f"authority registry {field} contains duplicate IDs")
    kubernetes_subjects = registry["kubernetes_identity_inventory"]
    if (
        not isinstance(kubernetes_subjects, list)
        or len(kubernetes_subjects) < 6
        or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "name",
                "category",
                "username",
                "groups",
                "credential_sha256",
                "provider_principal_id",
            }
            or item.get("category")
            not in {"owner", "workloads", "release", "human", "break-glass", "other"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("name", "username", "provider_principal_id")
            )
            or not isinstance(item.get("groups"), list)
            or not item["groups"]
            or any(not isinstance(group, str) or not group for group in item["groups"])
            or item["groups"] != sorted(set(item["groups"]))
            or not isinstance(item.get("credential_sha256"), str)
            or len(item["credential_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["credential_sha256"])
            for item in kubernetes_subjects
        )
        or {item["category"] for item in kubernetes_subjects}
        != {"owner", "workloads", "release", "human", "break-glass", "other"}
        or sum(item["category"] == "owner" for item in kubernetes_subjects) != 1
        or sum(item["category"] == "workloads" for item in kubernetes_subjects) != 1
        or len({item["name"] for item in kubernetes_subjects}) != len(kubernetes_subjects)
        or len({item["username"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
        or len({item["credential_sha256"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
        or len({item["provider_principal_id"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
    ):
        raise ValueError("authority registry Kubernetes identity inventory is incomplete")
    owner_identity = next(item for item in kubernetes_subjects if item["category"] == "owner")
    workloads_identity = next(
        item for item in kubernetes_subjects if item["category"] == "workloads"
    )
    if owner_identity["provider_principal_id"] != registry["authority_service_account_id"]:
        raise ValueError("Kubernetes owner is not bound to the provider authority principal")
    if workloads_identity["provider_principal_id"] != registry["workloads_service_account_id"]:
        raise ValueError("Kubernetes workloads identity is not bound to its provider principal")

    service_account_subjects = registry["kubernetes_service_account_inventory"]
    if (
        not isinstance(service_account_subjects, list)
        or not service_account_subjects
        or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "namespace",
                "name",
                "uid",
                "owner",
                "groups",
                "effective_authority_sha256",
                "dangerous_permissions",
            }
            or any(
                not isinstance(item[field], str) or not item[field]
                for field in ("namespace", "name", "uid", "owner")
            )
            or not isinstance(item.get("groups"), list)
            or item["groups"] != sorted(set(item["groups"]))
            or any(not isinstance(group, str) or not group for group in item["groups"])
            or not isinstance(item.get("effective_authority_sha256"), str)
            or len(item["effective_authority_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in item["effective_authority_sha256"]
            )
            or not isinstance(item.get("dangerous_permissions"), list)
            or item["dangerous_permissions"]
            != sorted(set(item["dangerous_permissions"]))
            or any(
                not isinstance(permission, str) or not permission
                for permission in item["dangerous_permissions"]
            )
            for item in service_account_subjects
        )
        or service_account_subjects
        != sorted(
            service_account_subjects,
            key=lambda item: (item["namespace"], item["name"], item["owner"]),
        )
        or len({(item["namespace"], item["name"]) for item in service_account_subjects})
        != len(service_account_subjects)
    ):
        raise ValueError("Kubernetes service-account subject inventory is incomplete")
    system_subjects = registry["kubernetes_system_subject_inventory"]
    if (
        not isinstance(system_subjects, list)
        or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "kind",
                "name",
                "uid",
                "namespace",
                "owner",
                "groups",
                "effective_authority_sha256",
                "dangerous_permissions",
            }
            or item.get("kind") not in {"User", "Group"}
            or not isinstance(item.get("name"), str)
            or not item["name"].startswith("system:")
            or item.get("namespace") != ""
            or not isinstance(item.get("uid"), str)
            or not item["uid"]
            or not isinstance(item.get("owner"), str)
            or not item["owner"]
            or not isinstance(item.get("groups"), list)
            or item["groups"] != sorted(set(item["groups"]))
            or not isinstance(item.get("effective_authority_sha256"), str)
            or len(item["effective_authority_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in item["effective_authority_sha256"]
            )
            or not isinstance(item.get("dangerous_permissions"), list)
            or item["dangerous_permissions"]
            != sorted(set(item["dangerous_permissions"]))
            or any(
                not isinstance(permission, str) or not permission
                for permission in item["dangerous_permissions"]
            )
            for item in system_subjects
        )
        or system_subjects
        != sorted(system_subjects, key=lambda item: (item["kind"], item["name"]))
        or len({(item["kind"], item["name"]) for item in system_subjects})
        != len(system_subjects)
    ):
        raise ValueError("Kubernetes native system-subject inventory is incomplete")
    controller_users = registry["kubernetes_controller_identities"]
    if (
        not isinstance(controller_users, dict)
        or set(controller_users) != CONTROLLER_ROLES
    ):
        raise ValueError("Kubernetes controller identities are incomplete or aliased")

    declared_controller_audit = registry["kubernetes_controller_audit_receipt"]
    if not isinstance(declared_controller_audit, dict):
        raise ValueError("controller audit receipt is absent from authority registry")
    controller_audit, controller_audit_sha256 = verify_live_controller_audit(
        str(declared_controller_audit.get("cluster_id", ""))
    )
    if controller_audit != declared_controller_audit:
        raise ValueError("live controller audit evidence differs from authority registry")
    if (
        not isinstance(controller_audit, dict)
        or set(controller_audit)
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
        or controller_audit.get("schema")
        != "fs2-serve.nebius.ai/kubernetes-controller-audit/v1"
        or controller_audit.get("controller_identities") != controller_users
    ):
        raise ValueError("signed controller audit receipt is absent or inconsistent")
    require_fresh_timestamp(
        controller_audit.get("observed_at"), "Kubernetes controller audit receipt"
    )
    controller_events = controller_audit.get("controller_events")
    if not isinstance(controller_events, dict) or set(controller_events) != CONTROLLER_ROLES:
        raise ValueError("controller audit events do not close every controller role")
    expected_controller_events = {
        "deployment": ("apps", "replicasets", "", {"create"}),
        "replicaset": ("", "pods", "", {"create"}),
        "daemonset": ("", "pods", "", {"create"}),
        "scheduler": ("", "pods", "binding", {"create"}),
        "node_health": ("", "nodes", "", {"update", "patch"}),
    }
    for role, event in controller_events.items():
        identity = controller_users[role]
        object_ref = event.get("object_ref") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict)
            or set(event)
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
            or not isinstance(object_ref, dict)
            or set(object_ref)
            != {"api_group", "resource", "subresource", "namespace", "name", "uid"}
            or object_ref.get("api_group") != expected_controller_events[role][0]
            or object_ref.get("resource") != expected_controller_events[role][1]
            or object_ref.get("subresource") != expected_controller_events[role][2]
            or event.get("verb") not in expected_controller_events[role][3]
            or not isinstance(object_ref.get("name"), str)
            or not object_ref["name"]
            or not isinstance(object_ref.get("uid"), str)
            or not object_ref["uid"]
            or event.get("user_info")
            != {
                "username": identity.get("username"),
                "uid": identity.get("uid"),
                "groups": identity.get("groups"),
            }
            or identity.get("audit_evidence_sha256")
            != hashlib.sha256(canonical(event)).hexdigest()
        ):
            raise ValueError(f"{role} identity is not bound to an authenticated audit event")
        require_fresh_timestamp(
            event.get("stage_timestamp"), f"{role} controller audit event"
        )
    node_health_mutation = controller_audit.get("node_health_mutation")
    if (
        not isinstance(node_health_mutation, dict)
        or set(node_health_mutation)
        != {"identity_role", "mutable_label_keys", "mutable_taint_keys", "allow_unschedulable"}
        or node_health_mutation.get("identity_role") != "node_health"
        or not isinstance(node_health_mutation.get("mutable_label_keys"), list)
        or node_health_mutation["mutable_label_keys"]
        != sorted(set(node_health_mutation["mutable_label_keys"]))
        or not isinstance(node_health_mutation.get("mutable_taint_keys"), list)
        or not node_health_mutation["mutable_taint_keys"]
        or node_health_mutation["mutable_taint_keys"]
        != sorted(set(node_health_mutation["mutable_taint_keys"]))
        or any(
            not isinstance(key, str)
            or not key
            or key.startswith("workload.fs2.nebius/")
            for key in node_health_mutation["mutable_label_keys"]
            + node_health_mutation["mutable_taint_keys"]
        )
        or node_health_mutation.get("allow_unschedulable") is not True
    ):
        raise ValueError("node health mutation contract is not exact and narrow")
    daemonset_maintainers = controller_audit.get("daemonset_maintainers")
    if not isinstance(daemonset_maintainers, dict) or not daemonset_maintainers:
        raise ValueError("audit-proven critical DaemonSet maintainers are absent")
    for key, maintainer in daemonset_maintainers.items():
        if not isinstance(key, str) or "/" not in key or not isinstance(maintainer, dict):
            raise ValueError("critical DaemonSet maintainer fields differ")
        namespace, name = key.split("/", 1)
        event = maintainer.get("event")
        identity = maintainer.get("identity")
        if (
            set(maintainer) != {"identity", "event", "event_sha256"}
            or not isinstance(event, dict)
            or not isinstance(identity, dict)
            or maintainer.get("event_sha256")
            != hashlib.sha256(canonical(event)).hexdigest()
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
                "username": identity.get("username"),
                "uid": identity.get("uid"),
                "groups": identity.get("groups"),
            }
        ):
            raise ValueError("critical DaemonSet maintainer lacks exact audit evidence")
        require_fresh_timestamp(
            event.get("stage_timestamp"), f"{key} maintainer audit event"
        )

    provisioning_receipts = registry["lane_provisioning_receipts"]
    if not isinstance(provisioning_receipts, dict) or not provisioning_receipts:
        raise ValueError("separately signed lane-provisioning receipts are absent")
    provisioning_receipt_digests: dict[str, str] = {}
    for provisioning_id, receipt in provisioning_receipts.items():
        legacy_fields = {
            "schema",
            "provisioning_generation",
            "authority_project_id",
            "cluster_id",
            "security_group_id",
            "node_group_id",
            "state_custody_sha256",
            "observed_at",
            "payload_sha256",
            "signature",
        }
        v2_fields = {
            "schema",
            "provisioning_generation",
            "manifest_sha256",
            "authority_project_id",
            "cluster_id",
            "security_group_id",
            "node_group_id",
            "backend_custody",
            "backend_custody_sha256",
            "provider_inventory",
            "provider_inventory_sha256",
            "custody_adapter_sha256",
            "observed_at",
            "payload_sha256",
            "signature",
        }
        v3_fields = v2_fields | {
            "expected_managed_addresses_sha256",
            "network_contract_sha256",
            "daemonset_admission_fence_receipt_sha256",
            "node_lifecycle_mode",
        }
        legacy = isinstance(receipt, dict) and set(receipt) == legacy_fields
        v2 = isinstance(receipt, dict) and set(receipt) == v2_fields
        v3 = isinstance(receipt, dict) and set(receipt) == v3_fields
        if (
            not isinstance(receipt, dict)
            or not (legacy or v2 or v3)
            or receipt.get("schema")
            != (
                "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v1"
                if legacy
                else (
                    "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v2"
                    if v2
                    else "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v3"
                )
            )
            or receipt.get("provisioning_generation") != provisioning_id
            or not re.fullmatch(r"p[0-9]{14}-[a-f0-9]{12}", provisioning_id)
            or receipt.get("authority_project_id") != registry["authority_project_id"]
            or not re.fullmatch(
                r"vpcsecuritygroup-[a-z0-9]+", str(receipt.get("security_group_id", ""))
            )
            or not re.fullmatch(
                r"mk8snodegroup-[a-z0-9]+", str(receipt.get("node_group_id", ""))
            )
        ):
            raise ValueError("lane-provisioning receipt identity differs")
        if legacy:
            digest(receipt.get("state_custody_sha256"), "lane provisioning state custody")
        else:
            backend = receipt.get("backend_custody")
            provider_inventory = receipt.get("provider_inventory")
            node_group = (
                provider_inventory.get("node_group")
                if isinstance(provider_inventory, dict)
                else None
            )
            if (
                not isinstance(backend, dict)
                or hashlib.sha256(canonical(backend)).hexdigest()
                != receipt.get("backend_custody_sha256")
                or not isinstance(backend.get("managed_addresses"), list)
                or not backend["managed_addresses"]
                or backend["managed_addresses"]
                != sorted(set(backend["managed_addresses"]))
                or not isinstance(backend.get("state_serial"), int)
                or backend["state_serial"] < 1
                or not isinstance(provider_inventory, dict)
                or hashlib.sha256(canonical(provider_inventory)).hexdigest()
                != receipt.get("provider_inventory_sha256")
                or provider_inventory.get("authority_project_id")
                != receipt.get("authority_project_id")
                or provider_inventory.get("cluster_id") != receipt.get("cluster_id")
                or not isinstance(node_group, dict)
                or node_group.get("id") != receipt.get("node_group_id")
                or node_group.get("min_node_count") != 1
                or node_group.get("max_node_count") != 1
                or not isinstance(node_group.get("members"), list)
                or not node_group["members"]
                or (v3 and len(node_group["members"]) != 1)
                or (
                    v3
                    and receipt.get("node_lifecycle_mode")
                    != "PARALLEL_GENERATIONAL_SINGLETON_CUTOVER_RETAIN_PREDECESSOR"
                )
            ):
                raise ValueError("lane provisioning live provider/backend custody differs")
            for field in (
                "manifest_sha256",
                "backend_custody_sha256",
                "provider_inventory_sha256",
                "custody_adapter_sha256",
            ):
                digest(receipt.get(field), f"lane provisioning {field}")
            if v3:
                digest(
                    receipt.get("expected_managed_addresses_sha256"),
                    "lane exact managed addresses",
                )
                digest(
                    receipt.get("network_contract_sha256"),
                    "lane network contract",
                )
                digest(
                    receipt.get("daemonset_admission_fence_receipt_sha256"),
                    "lane DaemonSet admission fence",
                )
            if (
                receipt.get("custody_adapter_sha256")
                != registry["lane_provisioning_custody_adapter_sha256"]
            ):
                raise ValueError("lane provisioning custody adapter differs from approval")
        provisioning_receipt_digests[provisioning_id] = verify_signed_object(
            receipt, public_key_pem=registry["checkpoint_public_key_pem"]
        )

    authority_graph = registry["provider_effective_authority_graph_receipt"]
    if not isinstance(authority_graph, dict) or set(authority_graph) != {
        "schema",
        "authority_root_resource_id",
        "project_id",
        "cluster_id",
        "snapshot_id",
        "snapshot_serial",
        "complete",
        "principals",
        "cluster_access_principal_ids",
        "mutating_principal_ids",
        "observed_at",
        "payload_sha256",
        "signature",
    }:
        raise ValueError("provider effective-authority graph receipt is absent")
    authority_graph_sha256 = verify_signed_object(
        authority_graph, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if authority_graph.get("schema") != (
        "fs2-serve.nebius.ai/provider-effective-authority-graph/v1"
    ):
        raise ValueError("provider effective-authority graph schema differs")
    if (
        authority_graph.get("project_id") != registry["authority_project_id"]
        or not isinstance(authority_graph.get("authority_root_resource_id"), str)
        or not authority_graph["authority_root_resource_id"]
        or not isinstance(authority_graph.get("cluster_id"), str)
        or not authority_graph["cluster_id"]
        or not isinstance(authority_graph.get("snapshot_id"), str)
        or not authority_graph["snapshot_id"]
        or not isinstance(authority_graph.get("snapshot_serial"), int)
        or authority_graph["snapshot_serial"] < 1
        or authority_graph.get("complete") is not True
    ):
        raise ValueError("provider effective-authority graph identity is incomplete")
    require_fresh_timestamp(authority_graph.get("observed_at"), "provider authority graph")
    graph_principals = authority_graph.get("principals")
    if (
        not isinstance(graph_principals, list)
        or not graph_principals
        or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "id",
                "kind",
                "origin_resource_ids",
                "cluster_access",
                "mutation_capabilities",
            }
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item.get("kind")
            not in {"service_account", "group", "user", "federated", "external"}
            or not isinstance(item.get("origin_resource_ids"), list)
            or not item["origin_resource_ids"]
            or item["origin_resource_ids"] != sorted(set(item["origin_resource_ids"]))
            or any(
                not isinstance(resource_id, str) or not resource_id
                for resource_id in item["origin_resource_ids"]
            )
            or not isinstance(item.get("cluster_access"), bool)
            or not isinstance(item.get("mutation_capabilities"), list)
            or item["mutation_capabilities"]
            != sorted(set(item["mutation_capabilities"]))
            or any(
                not isinstance(capability, str) or not capability
                for capability in item["mutation_capabilities"]
            )
            for item in graph_principals
        )
        or len({item["id"] for item in graph_principals}) != len(graph_principals)
    ):
        raise ValueError("provider effective-authority graph principals are incomplete")
    graph_cluster_access_ids = sorted(
        item["id"] for item in graph_principals if item["cluster_access"]
    )
    graph_mutating_ids = sorted(
        item["id"] for item in graph_principals if item["mutation_capabilities"]
    )
    if (
        authority_graph.get("cluster_access_principal_ids")
        != graph_cluster_access_ids
        or authority_graph.get("mutating_principal_ids") != graph_mutating_ids
        or graph_cluster_access_ids
        != sorted(item["provider_principal_id"] for item in kubernetes_subjects)
        or graph_mutating_ids != [registry["authority_group_id"]]
    ):
        raise ValueError(
            "provider-derived effective authority graph does not close cluster access and mutation"
        )

    iam_receipt = registry["provider_project_iam_inventory_receipt"]
    if not isinstance(iam_receipt, dict) or set(iam_receipt) != {
        "schema",
        "project_id",
        "inventory",
        "observed_at",
        "payload_sha256",
        "signature",
    }:
        raise ValueError("provider project IAM inventory receipt is absent")
    iam_receipt_sha256 = verify_signed_object(
        iam_receipt, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if iam_receipt.get("schema") != "fs2-serve.nebius.ai/provider-project-iam-inventory/v2":
        raise ValueError("provider project IAM inventory receipt schema differs")
    if iam_receipt.get("project_id") != registry["authority_project_id"]:
        raise ValueError("provider project IAM inventory receipt project differs")
    require_fresh_timestamp(iam_receipt.get("observed_at"), "provider IAM inventory")
    if not isinstance(iam_receipt.get("inventory"), dict):
        raise ValueError("provider project IAM inventory receipt has no exact inventory")
    inventory = iam_receipt["inventory"]
    if set(inventory) != {"groups", "service_accounts", "access_permits"}:
        raise ValueError("provider project IAM inventory fields differ")

    rbac_receipt = registry["kubernetes_rbac_inventory_receipt"]
    if not isinstance(rbac_receipt, dict):
        raise ValueError("Kubernetes RBAC inventory receipt is absent")
    rbac_receipt_sha256 = verify_signed_object(
        rbac_receipt, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if set(rbac_receipt) != {
        "schema",
        "cluster_id",
        "inventory_sha256",
        "subjects",
        "effective_authority",
        "blanket_tolerating_agents",
        "daemonset_list_resource_version",
        "daemonset_inventory_sha256",
        "controller_identities",
        "controller_audit_receipt_sha256",
        "observed_at",
        "payload_sha256",
        "signature",
    } or rbac_receipt.get("schema") != (
        "fs2-serve.nebius.ai/kubernetes-rbac-inventory/v6"
    ):
        raise ValueError("Kubernetes RBAC inventory receipt fields or schema differ")
    blanket_tolerating_agents = rbac_receipt.get("blanket_tolerating_agents")
    if (
        not isinstance(blanket_tolerating_agents, dict)
        or any(
            not isinstance(agent, dict)
            or set(agent)
            != {
                "namespace",
                "name",
                "uid",
                "snapshot_generation",
                "snapshot_sha256",
                "daemonset_spec",
                "daemonset_spec_sha256",
                "maintenance_identity",
                "maintenance_audit_sha256",
            }
            or not UID_RE.fullmatch(str(agent.get("uid", "")))
            or not re.fullmatch(
                r"s[0-9]{14}-[a-f0-9]{12}",
                str(agent.get("snapshot_generation", "")),
            )
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(agent.get("snapshot_sha256", ""))
            )
            or hashlib.sha256(canonical(agent.get("daemonset_spec"))).hexdigest()
            != agent.get("daemonset_spec_sha256")
            or not isinstance(agent.get("maintenance_identity"), dict)
            or not re.fullmatch(
                r"[a-f0-9]{64}", str(agent.get("maintenance_audit_sha256", ""))
            )
            for agent in blanket_tolerating_agents.values()
        )
        or not isinstance(rbac_receipt.get("daemonset_list_resource_version"), str)
        or not rbac_receipt["daemonset_list_resource_version"]
        or not re.fullmatch(
            r"[a-f0-9]{64}", str(rbac_receipt.get("daemonset_inventory_sha256", ""))
        )
    ):
        raise ValueError("complete live blanket-tolerating DaemonSet inventory is absent")
    if rbac_receipt.get("controller_identities") != controller_users:
        raise ValueError("signed RBAC receipt omits the audit-proven controller identities")
    if rbac_receipt.get("controller_audit_receipt_sha256") != controller_audit_sha256:
        raise ValueError("RBAC receipt is not bound to the signed controller audit artifact")
    audit_maintainers = controller_audit.get("daemonset_maintainers")
    if (
        not isinstance(audit_maintainers, dict)
        or set(audit_maintainers) != set(blanket_tolerating_agents)
        or any(
            not isinstance(audit_maintainers[key], dict)
            or set(audit_maintainers[key]) != {"identity", "event", "event_sha256"}
            or audit_maintainers[key].get("identity")
            != agent.get("maintenance_identity")
            or audit_maintainers[key].get("event_sha256")
            != agent.get("maintenance_audit_sha256")
            or audit_maintainers[key].get("event_sha256")
            != hashlib.sha256(canonical(audit_maintainers[key].get("event"))).hexdigest()
            for key, agent in blanket_tolerating_agents.items()
        )
    ):
        raise ValueError("blanket-agent maintainers differ from authenticated audit evidence")
    digest(rbac_receipt.get("inventory_sha256"), "Kubernetes RBAC inventory")
    require_fresh_timestamp(rbac_receipt.get("observed_at"), "Kubernetes RBAC inventory")
    declared_daemonset_fence = registry[
        "kubernetes_daemonset_admission_fence_receipt"
    ]
    if not isinstance(declared_daemonset_fence, dict):
        raise ValueError("continuous DaemonSet admission fence receipt is absent")
    daemonset_fence, daemonset_fence_sha256 = (
        verify_live_daemonset_admission_fence(
            str(rbac_receipt.get("cluster_id", "")),
            expected_inventory_sha256=str(
                rbac_receipt["daemonset_inventory_sha256"]
            ),
            expected_list_resource_version=str(
                rbac_receipt["daemonset_list_resource_version"]
            ),
            expected_agents=blanket_tolerating_agents,
            expected_controller_identity={
                field: controller_users["daemonset"][field]
                for field in ("username", "uid", "groups")
            },
        )
    )
    if daemonset_fence != declared_daemonset_fence:
        raise ValueError("live DaemonSet admission fence differs from authority registry")
    daemonset_snapshot_ledger_head_sha256 = str(
        daemonset_fence["snapshot_ledger_head_sha256"]
    )
    rbac_subjects = rbac_receipt.get("subjects")
    if (
        not isinstance(rbac_subjects, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"kind", "name", "namespace"}
            or item.get("kind") not in {"User", "Group", "ServiceAccount"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or not isinstance(item.get("namespace"), str)
            or (item["kind"] == "ServiceAccount" and not item["namespace"])
            or (item["kind"] != "ServiceAccount" and item["namespace"])
            for item in rbac_subjects
        )
        or rbac_subjects
        != sorted(
            rbac_subjects,
            key=lambda item: (item["kind"], item["namespace"], item["name"]),
        )
        or len({(item["kind"], item["namespace"], item["name"]) for item in rbac_subjects})
        != len(rbac_subjects)
    ):
        raise ValueError("Kubernetes RBAC subject inventory is malformed")
    effective_authority = rbac_receipt.get("effective_authority")
    if (
        not isinstance(effective_authority, list)
        or not effective_authority
        or any(
            not isinstance(item, dict)
            or set(item) != {"subject", "scope", "binding", "roleRef", "rules"}
            or item.get("subject") not in rbac_subjects
            or not isinstance(item.get("scope"), str)
            or not item["scope"]
            or not isinstance(item.get("binding"), dict)
            or not isinstance(item.get("roleRef"), dict)
            or not isinstance(item.get("rules"), list)
            for item in effective_authority
        )
        or effective_authority
        != sorted(
            effective_authority,
            key=lambda item: (
                item["subject"]["kind"],
                item["subject"]["namespace"],
                item["subject"]["name"],
                item["binding"]["kind"],
                item["binding"]["namespace"],
                item["binding"]["name"],
            ),
        )
    ):
        raise ValueError("Kubernetes RBAC effective-authority graph is malformed")
    rbac_effective_authority_sha256 = hashlib.sha256(
        canonical(effective_authority)
    ).hexdigest()
    authorized_users = {item["username"] for item in kubernetes_subjects}
    authorized_groups = {
        group for item in kubernetes_subjects for group in item["groups"]
    }
    authorized_groups.update(
        group for item in service_account_subjects for group in item["groups"]
    )
    authorized_service_accounts = {
        (item["namespace"], item["name"]) for item in service_account_subjects
    }
    authorized_users.update(
        item["name"] for item in system_subjects if item["kind"] == "User"
    )
    authorized_groups.update(
        item["name"] for item in system_subjects if item["kind"] == "Group"
    )
    authorized_groups.update(
        group
        for item in system_subjects
        if item["kind"] == "User"
        for group in item["groups"]
    )
    for subject in rbac_subjects:
        if subject["kind"] == "User" and subject["name"] not in authorized_users:
            raise ValueError("RBAC contains a User absent from the authority identity inventory")
        if subject["kind"] == "Group" and subject["name"] not in authorized_groups:
            raise ValueError("RBAC contains a Group absent from the authority identity inventory")
        if subject["kind"] == "ServiceAccount" and (
            subject["namespace"], subject["name"]
        ) not in authorized_service_accounts:
            raise ValueError(
                "RBAC contains a ServiceAccount absent from the authority identity inventory"
            )
    critical_daemonset_maintenance: dict[
        tuple[str, str, str], dict[str, set[str]]
    ] = {}
    for key, agent in blanket_tolerating_agents.items():
        identity = agent["maintenance_identity"]
        username = identity.get("username")
        match = re.fullmatch(
            r"system:serviceaccount:([^:]+):([^:]+)", str(username)
        )
        if match is None:
            raise ValueError("critical DaemonSet maintainer is not a ServiceAccount")
        if len(
            [
                subject
                for subject in service_account_subjects
                if subject.get("namespace") == match.group(1)
                and subject.get("name") == match.group(2)
                and subject.get("uid") == identity.get("uid")
                and subject.get("groups") == identity.get("groups")
            ]
        ) != 1:
            raise ValueError("critical DaemonSet maintainer is not a live signed subject")
        namespace, daemonset_name = key.split("/", 1)
        subject_key = ("ServiceAccount", match.group(1), match.group(2))
        critical_daemonset_maintenance.setdefault(subject_key, {}).setdefault(
            namespace, set()
        ).add(daemonset_name)
    verify_subject_inventory(
        service_account_subjects,
        system_subjects,
        effective_authority,
        controller_identities=controller_users,
        critical_daemonset_maintenance=critical_daemonset_maintenance,
    )

    custody = registry["accepted_custody"]
    if not isinstance(custody, dict) or set(custody) != {
        "sai10_commit",
        "sai10_tree",
        "independent_review_receipt_sha256",
    }:
        raise ValueError("accepted SAI-10 custody is incomplete")
    for field in ("sai10_commit", "sai10_tree"):
        if not isinstance(custody[field], str) or len(custody[field]) != 40:
            raise ValueError("accepted SAI-10 Git identity is invalid")
    digest(
        custody["independent_review_receipt_sha256"],
        "accepted SAI-10 review receipt",
    )
    if (
        custody["sai10_commit"] != ACCEPTED_SAI10_COMMIT
        or custody["sai10_tree"] != ACCEPTED_SAI10_TREE
        or not git_is_ancestor(ACCEPTED_SAI10_COMMIT, "HEAD")
    ):
        raise ValueError("customer storage is not based on accepted exact SAI-10 custody")

    expected_prior_fields = {
        "schema",
        "authority_project_id",
        "head_manifest_sha256",
        "head_generation_sha256",
        "provider_state_custody",
        "provider_state_custody_sha256",
        "boundary_state_custody",
        "boundary_state_custody_sha256",
        "live_custody",
        "live_custody_sha256",
        "observed_at",
        "payload_sha256",
        "signature",
    }
    if set(prior_head) != expected_prior_fields or prior_head.get("schema") != PRIOR_HEAD_SCHEMA:
        raise ValueError("authority prior-head fields or schema differ")
    prior_head_sha256 = verify_signed_object(
        prior_head, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if prior_head.get("authority_project_id") != registry["authority_project_id"]:
        raise ValueError("authority prior head project differs")
    digest(prior_head.get("head_manifest_sha256"), "prior manifest head")
    digest(prior_head.get("head_generation_sha256"), "prior generation head")
    digest(prior_head.get("provider_state_custody_sha256"), "provider state custody")
    digest(prior_head.get("boundary_state_custody_sha256"), "boundary state custody")
    digest(prior_head.get("live_custody_sha256"), "prior live custody")
    provider_state_custody = prior_head.get("provider_state_custody")
    provider_state_fields = {
        "backend_config_sha256",
        "backend_lineage",
        "state_lineage",
        "state_serial",
        "state_version_id",
        "state_version_adapter_sha256",
        "state_snapshot_sha256",
        "generation_chain_anchor_sha256",
        "generation_chain",
        "authority_gate_generations",
        "retained_generations",
        "managed_addresses",
    }
    provider_address_prefixes = (
        "terraform_data.external_authority",
        "nebius_vpc_v1_security_group.generation[",
        "nebius_vpc_v1_security_rule.private_ingress[",
        "nebius_vpc_v1_security_rule.dns_egress[",
        "nebius_vpc_v1_security_rule.database_egress[",
        "nebius_vpc_v1_security_rule.provider_egress[",
        "nebius_mk8s_v1_node_group.generation[",
        "nebius_vpc_v1_security_group.stable_lane[",
        "nebius_vpc_v1_security_rule.stable_private_ingress[",
        "nebius_vpc_v1_security_rule.stable_dns_egress[",
        "nebius_vpc_v1_security_rule.stable_database_egress[",
        "nebius_vpc_v1_security_rule.stable_provider_egress[",
        "nebius_mk8s_v1_node_group.stable_lane[",
    )
    if (
        not isinstance(provider_state_custody, dict)
        or set(provider_state_custody) != provider_state_fields
        or any(
            not isinstance(provider_state_custody.get(field), str)
            or not provider_state_custody[field]
            for field in (
                "backend_lineage",
                "state_lineage",
                "state_version_id",
            )
        )
        or not isinstance(provider_state_custody.get("state_serial"), int)
        or provider_state_custody["state_serial"] < 1
        or not isinstance(provider_state_custody.get("managed_addresses"), list)
        or not provider_state_custody["managed_addresses"]
        or provider_state_custody["managed_addresses"]
        != sorted(set(provider_state_custody["managed_addresses"]))
        or any(
            not isinstance(address, str)
            or not address.startswith(provider_address_prefixes)
            for address in provider_state_custody["managed_addresses"]
        )
        or not isinstance(provider_state_custody.get("generation_chain"), list)
        or not provider_state_custody["generation_chain"]
        or not isinstance(provider_state_custody.get("authority_gate_generations"), list)
        or provider_state_custody["authority_gate_generations"]
        != sorted(set(provider_state_custody["authority_gate_generations"]))
        or any(
            not isinstance(generation, str)
            or len(generation) != 28
            or not generation.startswith("g")
            for generation in provider_state_custody["authority_gate_generations"]
        )
        or not isinstance(provider_state_custody.get("retained_generations"), dict)
        or set(provider_state_custody["retained_generations"])
        != set(provider_state_custody["authority_gate_generations"])
    ):
        raise ValueError("provider state custody is not canonical and complete")
    retained_generations = provider_state_custody["retained_generations"]
    retained_generation_digests: dict[str, str] = {}
    protected_lane_ids: set[str] = set()
    for generation, retained in retained_generations.items():
        if (
            not isinstance(retained, dict)
            or frozenset(retained)
            not in {
                frozenset(LEGACY_GENERATION_FIELDS),
                frozenset(PROTECTED_LANE_V2_GENERATION_FIELDS),
                frozenset(PROTECTED_LANE_V3_GENERATION_FIELDS),
                frozenset(PROTECTED_LANE_V4_GENERATION_FIELDS),
                frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
                frozenset(GENERATION_FIELDS),
            }
            or retained.get("generation") != generation
        ):
            raise ValueError("provider retained-generation payload fields differ")
        content = {key: value for key, value in retained.items() if key != "generation"}
        content_sha256 = hashlib.sha256(canonical(content)).hexdigest()
        if generation[-12:] != content_sha256[:12]:
            raise ValueError("provider retained generation is not content-bound")
        host_routes(retained.get("provider_api_cidrs"), "retained provider API CIDRs")
        host_routes(
            retained.get("kubernetes_api_cidrs"), "retained Kubernetes API CIDRs"
        )
        host_routes(
            retained.get("bootstrap_https_cidrs"), "retained bootstrap HTTPS CIDRs"
        )
        private_routes(retained.get("private_cidrs"))
        if frozenset(retained) in {
            frozenset(PROTECTED_LANE_V2_GENERATION_FIELDS),
            frozenset(PROTECTED_LANE_V3_GENERATION_FIELDS),
            frozenset(PROTECTED_LANE_V4_GENERATION_FIELDS),
            frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
            frozenset(GENERATION_FIELDS),
        }:
            lane_id = retained.get("lane_id")
            if (
                not isinstance(lane_id, str)
                or not re.fullmatch(r"l[0-9]{14}-[a-f0-9]{12}", lane_id)
                or lane_id in protected_lane_ids
            ):
                raise ValueError("provider retained protected-lane ID is invalid or reused")
            protected_lane_ids.add(lane_id)
            expected_scheduling_key = (
                f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
            )
            observers = protected_observers(retained.get("protected_observers"), lane_id)
            node_names = protected_node_names(retained.get("protected_node_names"))
            node_labels = (
                protected_node_scheduling_labels(
                    retained.get("protected_node_scheduling_labels"),
                    node_names=node_names,
                    scheduling_key=expected_scheduling_key,
                    lane_id=lane_id,
                )
                if frozenset(retained)
                in {
                    frozenset(PROTECTED_LANE_V3_GENERATION_FIELDS),
                    frozenset(PROTECTED_LANE_V4_GENERATION_FIELDS),
                    frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
                    frozenset(GENERATION_FIELDS),
                }
                else None
            )
            node_attestations = (
                protected_node_attestations(
                    retained.get("protected_node_attestations"),
                    node_names=node_names,
                    scheduling_labels=node_labels,
                    scheduling_key=expected_scheduling_key,
                    lane_id=lane_id,
                )
                if frozenset(retained)
                == frozenset(PROTECTED_LANE_V4_GENERATION_FIELDS)
                and node_labels is not None
                else None
            )
            if frozenset(retained) in {
                frozenset(PROTECTED_LANE_V4_GENERATION_FIELDS),
                frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
                frozenset(GENERATION_FIELDS),
            }:
                retained_provisioning_id = provisioning_generation(retained)
                retained_provisioning_receipt = provisioning_receipts.get(
                    retained_provisioning_id
                )
                if (
                    retained_provisioning_receipt is None
                    or retained.get("provisioning_receipt_sha256")
                    != provisioning_receipt_digests.get(retained_provisioning_id)
                    or retained.get("security_group_id")
                    != retained_provisioning_receipt.get("security_group_id")
                    or retained.get("node_group_id")
                    != retained_provisioning_receipt.get("node_group_id")
                ):
                    raise ValueError("retained stable lane provisioning receipt differs")
                if frozenset(retained) in {
                    frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
                    frozenset(GENERATION_FIELDS),
                }:
                    expected_receipt_schema = (
                        "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v3"
                        if frozenset(retained) == frozenset(GENERATION_FIELDS)
                        else "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v2"
                    )
                    if (
                        retained_provisioning_receipt.get("schema")
                        != expected_receipt_schema
                    ):
                        raise ValueError(
                            "retained lane provider/backend custody schema differs"
                        )
                    node_attestations = protected_node_attestations(
                        retained.get("protected_node_attestations"),
                        node_names=node_names,
                        scheduling_labels=node_labels,
                        scheduling_key=expected_scheduling_key,
                        lane_id=lane_id,
                        provisioning_receipt=retained_provisioning_receipt,
                        provisioning_receipt_sha256=retained.get(
                            "provisioning_receipt_sha256"
                        ),
                    )
                if (
                    frozenset(retained)
                    == frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS)
                ):
                    # Pre-fence Deny policies remain active forever.  Migration
                    # therefore adopts each live blanket DaemonSet without an
                    # object update: exact namespace/name/UID/spec/maintainer
                    # must equal the independently inventoried agent already
                    # admitted by every retained policy.  The v3 external fence
                    # then protects CREATE/UPDATE/DELETE and exact child Pods.
                    for observer in observers.values():
                        if observer.get("class") != "critical-blanket-agent":
                            continue
                        key = f"{observer['namespace']}/{observer['name']}"
                        live_agent = blanket_tolerating_agents.get(key)
                        if (
                            not isinstance(live_agent, dict)
                            or live_agent.get("uid") != observer.get("uid")
                            or live_agent.get("daemonset_spec")
                            != observer.get("daemonset_spec")
                            or live_agent.get("daemonset_spec_sha256")
                            != observer.get("daemonset_spec_sha256")
                            or live_agent.get("maintenance_identity")
                            != observer.get("owner_identity")
                        ):
                            raise ValueError(
                                "retained blanket DaemonSet cannot be adopted "
                                "unchanged into the continuous fence"
                            )
            if (
                retained.get("scheduling_key") != expected_scheduling_key
                or retained.get("protected_observer_inventory_sha256")
                != hashlib.sha256(canonical(observers)).hexdigest()
                or retained.get("protected_node_inventory_sha256")
                != hashlib.sha256(canonical(node_names)).hexdigest()
                or (
                    node_labels is not None
                    and retained.get("protected_node_scheduling_labels_sha256")
                    != hashlib.sha256(canonical(node_labels)).hexdigest()
                )
                or (
                    node_attestations is not None
                    and retained.get("protected_node_attestation_sha256")
                    != hashlib.sha256(canonical(node_attestations)).hexdigest()
                )
                or not {
                    (
                        observer["owner_identity"]["username"]
                        if "owner_identity" in observer
                        else observer["owner_username"]
                    )
                    for observer in observers.values()
                }
                <= {
                    identity["username"]
                    for identity in kubernetes_subjects
                    if identity["category"] == "release"
                }
                or retained.get("min_node_count")
                != (
                    1
                    if frozenset(retained)
                    in {
                        frozenset(PROTECTED_LANE_V5_GENERATION_FIELDS),
                        frozenset(GENERATION_FIELDS),
                    }
                    else 0
                )
                or retained.get("max_node_count") != 1
            ):
                raise ValueError("provider retained protected-lane custody differs")
        if (
            not isinstance(retained.get("platform"), str)
            or not retained["platform"]
            or not isinstance(retained.get("preset"), str)
            or not retained["preset"]
            or retained.get("boot_disk_type")
            not in {"NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED"}
            or not isinstance(retained.get("boot_disk_gib"), int)
            or not 32 <= retained["boot_disk_gib"] <= 256
        ):
            raise ValueError("provider retained generation node shape is invalid")
        retained_generation_digests[generation] = content_sha256
    installed_generation_names: list[str] = []
    installed_predecessor: str | None = None
    for index, installed in enumerate(provider_state_custody["generation_chain"]):
        if (
            not isinstance(installed, dict)
            or set(installed) != {"generation", "predecessor_sha256", "content_sha256"}
        ):
            raise ValueError("provider installed-generation chain fields differ")
        generation = installed.get("generation")
        predecessor_sha256 = installed.get("predecessor_sha256")
        content_sha256 = installed.get("content_sha256")
        if (
            not isinstance(generation, str)
            or len(generation) != 28
            or not generation.startswith("g")
            or generation in installed_generation_names
        ):
            raise ValueError("provider installed-generation identity is invalid")
        digest(predecessor_sha256, "provider installed-generation predecessor")
        digest(content_sha256, "provider installed-generation content")
        if generation[-12:] != content_sha256[:12]:
            raise ValueError("provider installed generation is not content-bound")
        expected_predecessor = (
            provider_state_custody["generation_chain_anchor_sha256"]
            if index == 0
            else installed_predecessor
        )
        if predecessor_sha256 != expected_predecessor:
            raise ValueError("provider installed-generation chain is discontinuous")
        installed_generation_names.append(generation)
        installed_predecessor = content_sha256
        if retained_generation_digests.get(generation) != content_sha256:
            raise ValueError(
                "provider retained-generation payload differs from installed chain"
            )
    if installed_predecessor != prior_head["head_generation_sha256"]:
        raise ValueError("provider installed-generation chain does not end at prior head")
    for field in (
        "backend_config_sha256",
        "backend_lineage",
        "state_snapshot_sha256",
        "state_version_adapter_sha256",
        "generation_chain_anchor_sha256",
    ):
        digest(provider_state_custody[field], f"provider {field}")
    if (
        hashlib.sha256(canonical(provider_state_custody)).hexdigest()
        != prior_head["provider_state_custody_sha256"]
    ):
        raise ValueError("provider state custody digest differs from its signed content")
    boundary_state_custody = prior_head.get("boundary_state_custody")
    boundary_state_fields = {
        "backend_config_sha256",
        "backend_lineage",
        "state_lineage",
        "state_serial",
        "state_version_id",
        "state_version_adapter_sha256",
        "state_snapshot_sha256",
        "retained_legacy_boundary_policies",
        "retained_legacy_workload_policies",
        "retained_v3_boundary_policies",
        "retained_v3_workload_policies",
        "managed_addresses",
    }
    boundary_address_prefixes = (
        "terraform_data.separate_security_owner",
        "terraform_data.security_generation_v4[",
        "kubernetes_manifest.boundary_policy[",
        "kubernetes_manifest.boundary_binding[",
        "kubernetes_manifest.workload_policy[",
        "kubernetes_manifest.workload_binding[",
        "kubernetes_manifest.boundary_policy_v3[",
        "kubernetes_manifest.boundary_binding_v3[",
        "kubernetes_manifest.workload_policy_v3[",
        "kubernetes_manifest.workload_binding_v3[",
        "kubernetes_config_map_v1.trust[",
        "kubernetes_config_map_v1.contract[",
        "kubernetes_network_policy_v1.contract[",
        "kubernetes_config_map_v1.trust_v3[",
        "kubernetes_config_map_v1.contract_v3[",
        "kubernetes_network_policy_v1.contract_v3[",
        "kubernetes_role_v1.reconciler_inventory[",
        "kubernetes_role_binding_v1.reconciler_inventory[",
        "kubernetes_role_v1.reconciler_inventory_v3[",
        "kubernetes_role_binding_v1.reconciler_inventory_v3[",
        "helm_release.storage_reconciler_v2[",
        "helm_release.storage_reconciler_v3[",
    )
    if (
        not isinstance(boundary_state_custody, dict)
        or set(boundary_state_custody) != boundary_state_fields
        or any(
            not isinstance(boundary_state_custody.get(field), str)
            or not boundary_state_custody[field]
            for field in (
                "backend_lineage",
                "state_lineage",
                "state_version_id",
            )
        )
        or not isinstance(boundary_state_custody.get("state_serial"), int)
        or boundary_state_custody["state_serial"] < 1
        or not isinstance(boundary_state_custody.get("managed_addresses"), list)
        or not boundary_state_custody["managed_addresses"]
        or boundary_state_custody["managed_addresses"]
        != sorted(set(boundary_state_custody["managed_addresses"]))
        or any(
            not isinstance(address, str)
            or not address.startswith(boundary_address_prefixes)
            for address in boundary_state_custody["managed_addresses"]
        )
    ):
        raise ValueError("boundary state custody is not canonical and complete")
    retained_policy_sets: dict[str, dict[str, Any]] = {}
    for field, name_prefix, policy_address, binding_address in (
        (
            "retained_legacy_boundary_policies",
            "fs2-customer-storage-egress-boundary-",
            "kubernetes_manifest.boundary_policy[",
            "kubernetes_manifest.boundary_binding[",
        ),
        (
            "retained_legacy_workload_policies",
            "fs2-customer-storage-egress-boundary-workload-",
            "kubernetes_manifest.workload_policy[",
            "kubernetes_manifest.workload_binding[",
        ),
        (
            "retained_v3_boundary_policies",
            "fs2-storage-v3-boundary-",
            "kubernetes_manifest.boundary_policy_v3[",
            "kubernetes_manifest.boundary_binding_v3[",
        ),
        (
            "retained_v3_workload_policies",
            "fs2-storage-v3-workload-",
            "kubernetes_manifest.workload_policy_v3[",
            "kubernetes_manifest.workload_binding_v3[",
        ),
    ):
        retained = boundary_state_custody.get(field)
        if not isinstance(retained, dict):
            raise ValueError("retained admission custody is absent")
        for generation, policy in retained.items():
            if (
                not isinstance(generation, str)
                or len(generation) != 28
                or not generation.startswith("g")
                or not isinstance(policy, dict)
                or set(policy)
                != {"name", "policy_sha256", "policy_spec", "binding_spec"}
                or policy.get("name") != f"{name_prefix}{generation}"
                or policy.get("binding_spec")
                != {"policyName": policy.get("name"), "validationActions": ["Deny"]}
            ):
                raise ValueError("retained admission identity is malformed")
            policy_sha256 = digest(
                policy.get("policy_sha256"), "retained admission policy"
            )
            if (
                not isinstance(policy.get("policy_spec"), dict)
                or hashlib.sha256(canonical(policy["policy_spec"])).hexdigest()
                != policy_sha256
                or generation[-12:] != policy_sha256[:12]
            ):
                raise ValueError("retained admission policy is not content-bound")
            quoted = json.dumps(generation)
            required = {
                f"{policy_address}{quoted}]",
                f"{binding_address}{quoted}]",
            }
            if not required <= set(boundary_state_custody["managed_addresses"]):
                raise ValueError("retained policy/binding is absent from state custody")
        observed_policy_generations: set[str] = set()
        observed_binding_generations: set[str] = set()
        for address in boundary_state_custody["managed_addresses"]:
            for prefix, observed in (
                (policy_address, observed_policy_generations),
                (binding_address, observed_binding_generations),
            ):
                if not address.startswith(prefix):
                    continue
                if not address.endswith("]"):
                    raise ValueError("retained admission state address is malformed")
                try:
                    generation = json.loads(address[len(prefix) : -1])
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "retained admission state generation is malformed"
                    ) from exc
                if not isinstance(generation, str):
                    raise ValueError("retained admission generation is not a string")
                observed.add(generation)
        if (
            observed_policy_generations != set(retained)
            or observed_binding_generations != set(retained)
        ):
            raise ValueError(
                "retained admission custody does not cover every state-owned policy/binding"
            )
        retained_policy_sets[field] = retained
    for field in (
        "backend_config_sha256",
        "backend_lineage",
        "state_snapshot_sha256",
        "state_version_adapter_sha256",
    ):
        digest(boundary_state_custody[field], f"boundary {field}")
    if (
        hashlib.sha256(canonical(boundary_state_custody)).hexdigest()
        != prior_head["boundary_state_custody_sha256"]
    ):
        raise ValueError("boundary state custody digest differs from signed content")
    live_custody = prior_head.get("live_custody")
    expected_custody_fields = {
        "workloads_backend_id_sha256",
        "workloads_state_lineage",
        "workloads_state_serial",
        "managed_addresses",
        "predecessor_compatibility_sha256",
        "control_plane_release_id",
        "control_plane_release_revision",
        "control_plane_release_manifest_sha256",
        "predecessor_deployment_uid",
        "predecessor_deployment_spec_sha256",
        "predecessor_network_policy_uid",
        "predecessor_network_policy_spec_sha256",
    }
    if (
        not isinstance(live_custody, dict)
        or set(live_custody) != expected_custody_fields
        or not isinstance(live_custody.get("workloads_state_lineage"), str)
        or not live_custody["workloads_state_lineage"]
        or not isinstance(live_custody.get("workloads_state_serial"), int)
        or live_custody["workloads_state_serial"] < 1
        or live_custody.get("managed_addresses")
        != [
            "helm_release.control_plane",
            "kubernetes_config_map_v1.customer_storage_egress_contract[0]",
            "kubernetes_manifest.customer_storage_egress_admission_binding[0]",
            "kubernetes_manifest.customer_storage_egress_admission_policy[0]",
        ]
        or not isinstance(live_custody.get("control_plane_release_id"), str)
        or not live_custody["control_plane_release_id"]
        or not isinstance(live_custody.get("control_plane_release_revision"), int)
        or live_custody["control_plane_release_revision"] < 1
        or any(
            not isinstance(live_custody.get(field), str) or not live_custody[field]
            for field in (
                "predecessor_deployment_uid",
                "predecessor_network_policy_uid",
            )
        )
    ):
        raise ValueError("prior live custody does not retain the exact workloads state lineage")
    for field in (
        "workloads_backend_id_sha256",
        "control_plane_release_manifest_sha256",
        "predecessor_deployment_spec_sha256",
        "predecessor_network_policy_spec_sha256",
    ):
        digest(live_custody[field], field)
    digest(
        live_custody["predecessor_compatibility_sha256"],
        "custodied predecessor compatibility",
    )
    if hashlib.sha256(canonical(live_custody)).hexdigest() != prior_head["live_custody_sha256"]:
        raise ValueError("prior live custody digest differs from its signed content")
    try:
        observed_at = datetime.fromisoformat(
            str(prior_head["observed_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as exc:
        raise ValueError("authority prior-head timestamp is not RFC3339") from exc
    now = datetime.now(UTC)
    if observed_at > now or now - observed_at > timedelta(hours=24):
        raise ValueError("authority prior-head/state checkpoint is future-dated or stale")

    manifest = strict_json(manifest_json, "authority manifest")
    expected_manifest_fields = {
        "schema",
        "authority_project_id",
        "cluster_id",
        "network_id",
        "subnet_id",
        "node_service_account_id",
        "kubernetes_version",
        "generations",
        "current_generation",
        "parent_manifest_sha256",
        "parent_generation_sha256",
        "provider_state_custody_sha256",
        "boundary_state_custody_sha256",
        "prior_live_custody_sha256",
        "nebius_terraform_provider_version",
        "accepted_custody",
        "provider_project_iam_inventory_receipt_sha256",
        "provider_effective_authority_graph_receipt_sha256",
        "kubernetes_rbac_inventory_receipt_sha256",
        "payload_sha256",
        "signature",
    }
    if set(manifest) != expected_manifest_fields or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("authority manifest fields or schema differ")
    manifest_digest = verify_signed_object(
        manifest, public_key_pem=registry["manifest_public_key_pem"]
    )
    if registry["approved_manifest_sha256"] != manifest_digest:
        raise ValueError("authority manifest is not the exact externally approved ledger")
    cutover_state = load_cutover_state()
    cutover_receipt_sha256 = cutover_state["registry_anchor_sha256"]
    cutover_sources = {
        name: _source_sha256(Path(__file__).with_name(name))
        for name in (
            "storage_reconciler_cutover_policy.py",
            "storage_reconciler_cutover_runtime.py",
            "storage_reconciler_cutover_server.py",
            "storage_reconciler_cutover_executor.py",
        )
    }
    if (
        cutover_state.get("cluster_id") != manifest.get("cluster_id")
        or cutover_state.get("authority_manifest_sha256") != manifest_digest
        or cutover_state.get("source_bundle_sha256")
        != hashlib.sha256(canonical(cutover_sources)).hexdigest()
    ):
        raise ValueError("live reconciler cutover state differs from the authority manifest")
    if manifest.get("authority_project_id") != registry["authority_project_id"]:
        raise ValueError("authority manifest project differs from the external registry")
    if not isinstance(manifest.get("cluster_id"), str) or not manifest["cluster_id"]:
        raise ValueError("customer-storage authority requires the exact target cluster")
    if controller_audit.get("cluster_id") != manifest["cluster_id"]:
        raise ValueError("controller audit receipt belongs to a different cluster")
    if manifest.get("parent_manifest_sha256") != prior_head["head_manifest_sha256"]:
        raise ValueError("authority manifest does not extend the separately anchored prior head")
    if manifest.get("parent_generation_sha256") != prior_head["head_generation_sha256"]:
        raise ValueError("authority manifest does not extend the anchored generation chain")
    if (
        manifest.get("provider_state_custody_sha256")
        != prior_head["provider_state_custody_sha256"]
    ):
        raise ValueError("authority manifest does not retain canonical provider state custody")
    if (
        manifest.get("boundary_state_custody_sha256")
        != prior_head["boundary_state_custody_sha256"]
    ):
        raise ValueError("authority manifest does not retain canonical boundary state custody")
    if manifest.get("prior_live_custody_sha256") != prior_head["live_custody_sha256"]:
        raise ValueError("authority manifest does not retain the prior live custody")
    if manifest.get("nebius_terraform_provider_version") != NEBIUS_TERRAFORM_PROVIDER_VERSION:
        raise ValueError("authority manifest uses a different Terraform provider version")
    if manifest.get("accepted_custody") != custody:
        raise ValueError("authority manifest does not bind the accepted SAI-10 custody")
    if manifest.get("provider_project_iam_inventory_receipt_sha256") != iam_receipt_sha256:
        raise ValueError("authority manifest does not bind the exact provider IAM receipt")
    if (
        manifest.get("provider_effective_authority_graph_receipt_sha256")
        != authority_graph_sha256
        or authority_graph.get("cluster_id") != manifest["cluster_id"]
    ):
        raise ValueError("authority manifest does not bind the provider authority graph")
    if (
        manifest.get("kubernetes_rbac_inventory_receipt_sha256")
        != rbac_receipt_sha256
        or rbac_receipt.get("cluster_id") != manifest["cluster_id"]
    ):
        raise ValueError("authority manifest does not bind the target-cluster RBAC receipt")

    generations = manifest.get("generations")
    if not isinstance(generations, list) or not generations:
        raise ValueError("authority manifest needs at least one generation")
    normalized: dict[str, dict[str, Any]] = {}
    predecessor = prior_head["head_generation_sha256"]
    for entry in generations:
        if not isinstance(entry, dict) or set(entry) != GENERATION_FIELDS:
            raise ValueError("authority generation fields differ")
        content = {key: value for key, value in entry.items() if key != "generation"}
        content_digest = hashlib.sha256(canonical(content)).hexdigest()
        generation = entry.get("generation")
        if (
            not isinstance(generation, str)
            or not generation.startswith("g")
            or len(generation) != 28
            or generation[-12:] != content_digest[:12]
            or generation in normalized
        ):
            raise ValueError("authority generation is not content-bound and unique")
        if entry.get("predecessor_sha256") != predecessor:
            raise ValueError("authority generation chain is incomplete or reordered")
        for field in (
            "contract_sha256",
            "predecessor_compatibility_sha256",
            "boundary_policy_sha256",
            "workload_policy_sha256",
            "release_values_sha256",
            "provider_iam_receipt_sha256",
            "provider_authority_graph_receipt_sha256",
            "kubernetes_rbac_receipt_sha256",
            "provider_state_custody_sha256",
            "boundary_state_custody_sha256",
            "prior_live_custody_sha256",
            "accepted_sai10_review_sha256",
            "protected_observer_inventory_sha256",
            "protected_node_scheduling_labels_sha256",
            "protected_node_attestation_sha256",
            "provisioning_receipt_sha256",
            "controller_audit_receipt_sha256",
            "daemonset_inventory_sha256",
            "daemonset_admission_fence_receipt_sha256",
            "daemonset_snapshot_ledger_head_sha256",
            "reconciler_cutover_receipt_sha256",
        ):
            value = entry.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"authority generation {field} is invalid")
        if (
            entry.get("provider_iam_receipt_sha256") != iam_receipt_sha256
            or entry.get("provider_authority_graph_receipt_sha256")
            != authority_graph_sha256
            or entry.get("kubernetes_rbac_receipt_sha256") != rbac_receipt_sha256
            or entry.get("provider_state_custody_sha256")
            != prior_head["provider_state_custody_sha256"]
            or entry.get("boundary_state_custody_sha256")
            != prior_head["boundary_state_custody_sha256"]
            or entry.get("prior_live_custody_sha256")
            != prior_head["live_custody_sha256"]
            or entry.get("accepted_sai10_commit") != custody["sai10_commit"]
            or entry.get("accepted_sai10_tree") != custody["sai10_tree"]
            or entry.get("accepted_sai10_review_sha256")
            != custody["independent_review_receipt_sha256"]
            or entry.get("cluster_id") != manifest["cluster_id"]
            or entry.get("network_id") != manifest["network_id"]
            or entry.get("subnet_id") != manifest["subnet_id"]
            or entry.get("node_service_account_id")
            != manifest["node_service_account_id"]
            or entry.get("kubernetes_version") != manifest["kubernetes_version"]
            or entry.get("nebius_terraform_provider_version")
            != NEBIUS_TERRAFORM_PROVIDER_VERSION
        ):
            raise ValueError(
                "authority generation is not content-bound to custody and provider parents"
            )
        if (
            not isinstance(entry.get("daemonset_list_resource_version"), str)
            or not entry["daemonset_list_resource_version"]
        ):
            raise ValueError("authority generation omits the exact DaemonSet list revision")
        host_routes(entry.get("provider_api_cidrs"), "provider API CIDRs")
        host_routes(entry.get("kubernetes_api_cidrs"), "Kubernetes API CIDRs")
        host_routes(entry.get("bootstrap_https_cidrs"), "bootstrap HTTPS CIDRs")
        private_routes(entry.get("private_cidrs"))
        lane_id = entry.get("lane_id")
        if (
            not isinstance(lane_id, str)
            or not re.fullmatch(r"l[0-9]{14}-[a-f0-9]{12}", lane_id)
            or lane_id in protected_lane_ids
        ):
            raise ValueError("authority generation protected-lane ID is invalid or reused")
        protected_lane_ids.add(lane_id)
        expected_scheduling_key = (
            f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
        )
        observers = protected_observers(entry.get("protected_observers"), lane_id)
        node_names = protected_node_names(entry.get("protected_node_names"))
        node_labels = protected_node_scheduling_labels(
            entry.get("protected_node_scheduling_labels"),
            node_names=node_names,
            scheduling_key=expected_scheduling_key,
            lane_id=lane_id,
        )
        stable_provisioning_generation = provisioning_generation(entry)
        provisioning_receipt = provisioning_receipts.get(stable_provisioning_generation)
        if (
            not isinstance(provisioning_receipt, dict)
            or provisioning_receipt.get("schema")
            != "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v3"
        ):
            raise ValueError("current generation requires semantic lane custody v3")
        require_fresh_timestamp(
            provisioning_receipt.get("observed_at"), "lane provisioning receipt"
        )
        verify_live_lane_custody(
            provisioning_receipt,
            registry["lane_provisioning_custody_adapter_sha256"],
        )
        verify_lane_network_contract(
            provisioning_receipt,
            entry,
            set(provisioning_receipts),
        )
        node_attestations = protected_node_attestations(
            entry.get("protected_node_attestations"),
            node_names=node_names,
            scheduling_labels=node_labels,
            scheduling_key=expected_scheduling_key,
            lane_id=lane_id,
            provisioning_receipt=provisioning_receipt,
            provisioning_receipt_sha256=entry.get("provisioning_receipt_sha256"),
        )
        signed_blanket_agents = {
            f"{observer['namespace']}/{observer['name']}": {
                "namespace": observer["namespace"],
                "name": observer["name"],
                "uid": observer["uid"],
                "snapshot_generation": observer["snapshot_generation"],
                "snapshot_sha256": observer["snapshot_sha256"],
                "daemonset_spec": observer["daemonset_spec"],
                "daemonset_spec_sha256": observer["daemonset_spec_sha256"],
                "maintenance_identity": observer["owner_identity"],
                "maintenance_audit_sha256": observer[
                    "maintenance_audit_sha256"
                ],
            }
            for observer in observers.values()
            if observer["class"] == "critical-blanket-agent"
        }
        if (
            entry.get("scheduling_key") != expected_scheduling_key
            or entry.get("protected_observer_inventory_sha256")
            != hashlib.sha256(canonical(observers)).hexdigest()
            or entry.get("protected_node_inventory_sha256")
            != hashlib.sha256(canonical(node_names)).hexdigest()
            or entry.get("protected_node_scheduling_labels_sha256")
            != hashlib.sha256(canonical(node_labels)).hexdigest()
            or entry.get("protected_node_attestation_sha256")
            != hashlib.sha256(canonical(node_attestations)).hexdigest()
            or provisioning_receipt is None
            or entry.get("provisioning_receipt_sha256")
            != provisioning_receipt_digests.get(stable_provisioning_generation)
            or entry.get("security_group_id")
            != provisioning_receipt.get("security_group_id")
            or entry.get("node_group_id") != provisioning_receipt.get("node_group_id")
            or provisioning_receipt.get("cluster_id") != manifest["cluster_id"]
            or signed_blanket_agents != blanket_tolerating_agents
            or entry.get("controller_audit_receipt_sha256")
            != controller_audit_sha256
            or entry.get("daemonset_inventory_sha256")
            != rbac_receipt.get("daemonset_inventory_sha256")
            or entry.get("daemonset_list_resource_version")
            != rbac_receipt.get("daemonset_list_resource_version")
            or entry.get("daemonset_admission_fence_receipt_sha256")
            != daemonset_fence_sha256
            or provisioning_receipt.get(
                "daemonset_admission_fence_receipt_sha256"
            )
            != daemonset_fence_sha256
            or entry.get("daemonset_snapshot_ledger_head_sha256")
            != daemonset_snapshot_ledger_head_sha256
            or entry.get("node_health_mutation") != node_health_mutation
            or entry.get("node_lifecycle_mode")
            != "PARALLEL_GENERATIONAL_SINGLETON_CUTOVER_RETAIN_PREDECESSOR"
            or not all(
                observer["owner_identity"]
                in [
                    {
                        "username": f"system:serviceaccount:{subject['namespace']}:{subject['name']}",
                        "uid": subject["uid"],
                        "groups": subject["groups"],
                    }
                    for subject in service_account_subjects
                ]
                for observer in observers.values()
            )
            or entry.get("min_node_count") != 1
            or entry.get("max_node_count") != 1
            or not re.fullmatch(
                r"https://[^/]+/v1/storage-reconciler/activation",
                str(entry.get("reconciler_activation_endpoint", "")),
            )
            or not re.fullmatch(
                r"fs2-storage-activation-trust-r[0-9]{14}-[a-f0-9]{12}",
                str(entry.get("reconciler_activation_public_key_config_map_name", "")),
            )
            or not re.fullmatch(
                r"fs2-storage-activation-ca-r[0-9]{14}-[a-f0-9]{12}",
                str(entry.get("reconciler_activation_ca_config_map_name", "")),
            )
            or not isinstance(entry.get("reconciler_activation_minimum_epoch"), int)
            or entry["reconciler_activation_minimum_epoch"] < 1
        ):
            raise ValueError("authority generation protected-lane custody differs")
        if (
            not isinstance(entry.get("platform"), str)
            or not entry["platform"]
            or not isinstance(entry.get("preset"), str)
            or not entry["preset"]
            or entry.get("boot_disk_type") not in {"NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED"}
            or not isinstance(entry.get("boot_disk_gib"), int)
            or not 32 <= entry["boot_disk_gib"] <= 256
        ):
            raise ValueError("authority generation node shape is invalid")
        normalized[generation] = entry
        predecessor = content_digest
    if manifest.get("current_generation") != list(normalized)[-1]:
        raise ValueError("current authority generation must be the final signed generation")
    current_entry = normalized[manifest["current_generation"]]
    transition_generations = {
        value
        for value in (
            cutover_state.get("transition", {}).get("predecessor_generation"),
            cutover_state.get("transition", {}).get("successor_generation"),
        )
        if isinstance(value, str)
    }
    if (
        current_entry.get("reconciler_cutover_receipt_sha256")
        != cutover_receipt_sha256
        or (
            cutover_state.get("active_generation") is not None
            and cutover_state.get("active_generation") not in transition_generations
        )
        or (
            cutover_state.get("active_generation") is None
            and cutover_state.get("transition", {}).get("phase")
            not in {
                "QUIESCE_PREDECESSOR",
                "PREDECESSOR_QUIESCED",
                "ROLLBACK_QUIESCE",
                "ROLLBACK_SUCCESSOR_QUIESCED",
            }
        )
    ):
        raise ValueError("authority head does not bind the live signed reconciler cutover")
    expected_provider_addresses = ["terraform_data.external_authority"]
    expected_provider_addresses.extend(
        f"terraform_data.external_authority_v4[{json.dumps(generation)}]"
        for generation in provider_state_custody["authority_gate_generations"]
    )
    manifest_generation_order = list(normalized)
    # The signed prior-state chain contains only installed generations and
    # terminates at prior_head.head_generation_sha256. The successor-only
    # manifest begins from that exact hash (checked above); names need only be
    # disjoint because their suffixes bind content, not chronological order.
    if set(retained_generations) & set(manifest_generation_order):
        raise ValueError("provider successor generations overlap prior custody")
    for generation in installed_generation_names:
        retained = retained_generations[generation]
        if "provisioning_generation" in retained:
            # Stable-lane resources live in the separately custodied
            # provisioning root and are bound here only by its signed receipt.
            continue
        else:
            quoted = json.dumps(generation)
            expected_provider_addresses.extend(
                [
                    f"nebius_mk8s_v1_node_group.generation[{quoted}]",
                    f"nebius_vpc_v1_security_group.generation[{quoted}]",
                    f"nebius_vpc_v1_security_rule.database_egress[{quoted}]",
                    f"nebius_vpc_v1_security_rule.dns_egress[{quoted}]",
                    f"nebius_vpc_v1_security_rule.private_ingress[{quoted}]",
                    f"nebius_vpc_v1_security_rule.provider_egress[{quoted}]",
                ]
            )
    required_provider_addresses = set(expected_provider_addresses)
    observed_provider_addresses = set(provider_state_custody["managed_addresses"])
    allowed_pending_provider_keys = set(provider_state_custody["authority_gate_generations"])
    allowed_pending_provider_keys.update(
        retained["provisioning_generation"]
        for retained in retained_generations.values()
        if "provisioning_generation" in retained
    )
    allowed_pending_provider_keys.update(
        entry["provisioning_generation"] for entry in normalized.values()
    )
    if not required_provider_addresses <= observed_provider_addresses:
        raise ValueError("provider state custody omits a required managed address")
    for address in sorted(observed_provider_addresses - required_provider_addresses):
        if not address.endswith("]") or "[" not in address:
            raise ValueError("provider state custody contains an unbound address")
        try:
            pending_generation = json.loads(address.rsplit("[", 1)[1][:-1])
        except json.JSONDecodeError as exc:
            raise ValueError("provider pending address generation is malformed") from exc
        if (
            not isinstance(pending_generation, str)
            or pending_generation not in allowed_pending_provider_keys
            or not address.startswith(provider_address_prefixes[1:])
        ):
            raise ValueError("provider pending address is not bound to a retained gate")

    return {
        "authorized": "true",
        "manifest_sha256": manifest_digest,
        "prior_head_receipt_sha256": prior_head_sha256,
        "prior_live_custody_sha256": prior_head["live_custody_sha256"],
        "prior_live_predecessor_compatibility_sha256": live_custody[
            "predecessor_compatibility_sha256"
        ],
        "manifest_json": json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        "generations_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        "retained_generations_json": json.dumps(
            retained_generations, sort_keys=True, separators=(",", ":")
        ),
        "authority_project_id": registry["authority_project_id"],
        "authority_service_account_id": registry["authority_service_account_id"],
        "authority_group_id": registry["authority_group_id"],
        "workloads_service_account_id": registry["workloads_service_account_id"],
        "kubernetes_identity_inventory_sha256": hashlib.sha256(
            canonical(sorted(kubernetes_subjects, key=lambda item: item["name"]))
        ).hexdigest(),
        "kubernetes_service_account_inventory_sha256": hashlib.sha256(
            canonical(service_account_subjects)
        ).hexdigest(),
        "kubernetes_system_subject_inventory_sha256": hashlib.sha256(
            canonical(system_subjects)
        ).hexdigest(),
        "controller_identities_json": json.dumps(
            controller_users, sort_keys=True, separators=(",", ":")
        ),
        "controller_audit_receipt_sha256": controller_audit_sha256,
        "node_health_mutation_json": json.dumps(
            node_health_mutation, sort_keys=True, separators=(",", ":")
        ),
        "daemonset_inventory_sha256": rbac_receipt["daemonset_inventory_sha256"],
        "daemonset_list_resource_version": rbac_receipt[
            "daemonset_list_resource_version"
        ],
        "daemonset_admission_fence_receipt_sha256": daemonset_fence_sha256,
        "daemonset_snapshot_ledger_head_sha256": (
            daemonset_snapshot_ledger_head_sha256
        ),
        "reconciler_cutover_receipt_sha256": cutover_receipt_sha256,
        "provider_project_iam_inventory_receipt_sha256": iam_receipt_sha256,
        "provider_effective_authority_graph_receipt_sha256": authority_graph_sha256,
        "provider_authority_adapter_sha256": registry["provider_authority_adapter_sha256"],
        "provider_state_custody_sha256": prior_head["provider_state_custody_sha256"],
        "prior_authority_gate_generations_json": json.dumps(
            provider_state_custody["authority_gate_generations"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "boundary_state_custody_sha256": prior_head["boundary_state_custody_sha256"],
        "retained_legacy_boundary_policies_json": json.dumps(
            retained_policy_sets["retained_legacy_boundary_policies"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "retained_legacy_workload_policies_json": json.dumps(
            retained_policy_sets["retained_legacy_workload_policies"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "retained_v3_boundary_policies_json": json.dumps(
            retained_policy_sets["retained_v3_boundary_policies"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "retained_v3_workload_policies_json": json.dumps(
            retained_policy_sets["retained_v3_workload_policies"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "retained_admission_custody_sha256": hashlib.sha256(
            canonical(retained_policy_sets)
        ).hexdigest(),
        "kubernetes_rbac_inventory_receipt_sha256": rbac_receipt_sha256,
        "kubernetes_rbac_inventory_sha256": rbac_receipt["inventory_sha256"],
        "kubernetes_rbac_effective_authority_sha256": rbac_effective_authority_sha256,
        "accepted_sai10_commit": custody["sai10_commit"],
        "accepted_sai10_tree": custody["sai10_tree"],
        "sai10_independent_review_receipt_sha256": custody[
            "independent_review_receipt_sha256"
        ],
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {"authority_manifest_json"}:
            raise ValueError("authority verifier query differs")
        manifest_json = query["authority_manifest_json"]
        if not isinstance(manifest_json, str) or not manifest_json:
            raise ValueError("authority manifest is absent")
        print(json.dumps(verify(manifest_json), sort_keys=True))
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"customer-storage provider authority rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
