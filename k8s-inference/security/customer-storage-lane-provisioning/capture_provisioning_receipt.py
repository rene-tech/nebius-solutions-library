#!/usr/bin/env python3
"""Capture provider/backend custody for an already-created bootstrap lane.

The fixed, root-owned adapter performs the provider API and remote-state reads.
This source only validates its descriptor identity and exact response before an
external security owner signs the emitted receipt.  It never mutates provider
or Terraform state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from verify_provisioning_manifest import (
    MAX_BYTES,
    REGISTRY,
    SCHEMA,
    canonical,
    safe_root_read,
    strict_json,
    verify_signed,
)

CUSTODY_ADAPTER = Path("/usr/libexec/fs2-security/lane-provisioning-custody")
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v3"
ADAPTER_SCHEMA = "fs2-serve.nebius.ai/protected-lane-provisioning-custody/v2"
MAX_OUTPUT_BYTES = 8 * MAX_BYTES


def _open_adapter(path: Path, expected_sha256: str) -> tuple[int, os.stat_result]:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
        )
    finally:
        os.close(directory)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    payload = os.read(descriptor, metadata.st_size + 1)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
        or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        or len(payload) != metadata.st_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        os.close(descriptor)
        raise ValueError("lane custody adapter is not the pinned read-only executable")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, metadata


def _safe_input(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
        )
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > MAX_BYTES:
            raise ValueError("provisioning manifest is not a bounded regular file")
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("provisioning manifest changed during its descriptor read")
        return payload
    finally:
        os.close(descriptor)


def _fresh(value: object, label: str, *, minutes: int = 5) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp is absent")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is not RFC3339") from exc
    now = datetime.now(UTC)
    if observed > now or now - observed > timedelta(minutes=minutes):
        raise ValueError(f"{label} is future-dated or stale")
    return observed.isoformat().replace("+00:00", "Z")


def _digest(value: object, label: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", str(value)):
        raise ValueError(f"{label} is not a SHA-256 digest")
    return str(value)


def _generation(manifest: dict[str, Any], generation: str) -> dict[str, Any]:
    generations = manifest.get("generations")
    if not isinstance(generations, list):
        raise ValueError("signed provisioning generations are absent")
    matches = [
        item
        for item in generations
        if isinstance(item, dict) and item.get("provisioning_generation") == generation
    ]
    if len(matches) != 1:
        raise ValueError("requested provisioning generation is not unique")
    return matches[0]


def _expected_managed_addresses(manifest: dict[str, Any]) -> list[str]:
    generations = manifest.get("generations")
    if not isinstance(generations, list) or not generations:
        raise ValueError("signed provisioning generations are absent")
    resources = (
        "terraform_data.signed_provisioning",
        "nebius_vpc_v1_security_group.lane",
        "nebius_vpc_v1_security_rule.private_ingress",
        "nebius_vpc_v1_security_rule.dns_egress",
        "nebius_vpc_v1_security_rule.database_egress",
        "nebius_vpc_v1_security_rule.provider_egress",
        "nebius_mk8s_v1_node_group.lane",
    )
    generation_ids = [
        item.get("provisioning_generation")
        for item in generations
        if isinstance(item, dict)
    ]
    if (
        len(generation_ids) != len(generations)
        or any(not isinstance(item, str) or not item for item in generation_ids)
        or generation_ids != list(dict.fromkeys(generation_ids))
    ):
        raise ValueError("provisioning generation address keys are ambiguous")
    return sorted(
        f"{resource}[{json.dumps(generation)}]"
        for generation in generation_ids
        for resource in resources
    )


def _expected_labels(generation: dict[str, Any]) -> dict[str, str]:
    return {
        "managed-by": "fs2-lane-security-owner",
        "security-boundary": "customer-storage-egress",
        "provisioning-generation": generation["provisioning_generation"],
        "lane-id": generation["lane_id"],
    }


def _expected_rules(generation: dict[str, Any]) -> list[dict[str, Any]]:
    labels = _expected_labels(generation)
    provider_cidrs = sorted(
        set(
            generation["provider_api_cidrs"]
            + generation["kubernetes_api_cidrs"]
            + generation["bootstrap_https_cidrs"]
        )
    )
    common = {
        "access": "ALLOW",
        "type": "STATEFUL",
        "priority": 100,
    }
    values = [
        {
            **common,
            "name": f"fs2-storage-private-{generation['provisioning_generation']}",
            "labels": {**labels, "purpose": "private-ingress"},
            "protocol": "ANY",
            "direction": "INGRESS",
            "source_cidrs": generation["private_cidrs"],
            "destination_cidrs": [],
            "destination_ports": [],
        },
        {
            **common,
            "name": f"fs2-storage-dns-{generation['provisioning_generation']}",
            "labels": {**labels, "purpose": "dns-egress"},
            "protocol": "ANY",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": generation["private_cidrs"],
            "destination_ports": [53],
        },
        {
            **common,
            "name": f"fs2-storage-db-{generation['provisioning_generation']}",
            "labels": {**labels, "purpose": "database-egress"},
            "protocol": "TCP",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": generation["private_cidrs"],
            "destination_ports": [5432],
        },
        {
            **common,
            "name": f"fs2-storage-provider-{generation['provisioning_generation']}",
            "labels": {**labels, "purpose": "provider-egress"},
            "protocol": "TCP",
            "direction": "EGRESS",
            "source_cidrs": [],
            "destination_cidrs": provider_cidrs,
            "destination_ports": [443],
        },
    ]
    return sorted(values, key=lambda item: item["name"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()

    registry = strict_json(safe_root_read(REGISTRY))
    if (
        set(registry)
        != {
            "schema",
            "approved_manifest_sha256",
            "manifest_public_key_pem",
            "checkpoint_public_key_pem",
            "custody_adapter_sha256",
            "daemonset_admission_fence_receipt_sha256",
        }
        or registry.get("schema")
        != "fs2-serve.nebius.ai/protected-lane-provisioning-registry/v3"
        or not re.fullmatch(
            r"[a-f0-9]{64}",
            str(registry.get("daemonset_admission_fence_receipt_sha256", "")),
        )
    ):
        raise ValueError("provisioning registry schema differs")
    manifest = strict_json(_safe_input(args.manifest))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("provisioning manifest schema differs")
    manifest_sha256 = verify_signed(manifest, str(registry["manifest_public_key_pem"]))
    if manifest_sha256 != registry.get("approved_manifest_sha256"):
        raise ValueError("provisioning manifest is not the approved signed object")
    generation = _generation(manifest, args.generation)

    adapter_sha256 = _digest(
        registry.get("custody_adapter_sha256"), "custody adapter"
    )
    descriptor, before = _open_adapter(CUSTODY_ADAPTER, adapter_sha256)
    request = {
        "schema": "fs2-serve.nebius.ai/protected-lane-provisioning-custody-request/v2",
        "manifest_sha256": manifest_sha256,
        "provisioning_generation": args.generation,
        "authority_project_id": generation["authority_project_id"],
        "cluster_id": generation["cluster_id"],
        "network_id": generation["network_id"],
        "lane_id": generation["lane_id"],
        "scheduling_key": generation["scheduling_key"],
    }
    try:
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
        or len(result.stdout.encode()) > MAX_OUTPUT_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("lane custody adapter failed or changed during capture")
    custody = strict_json(result.stdout)
    if set(custody) != {
        "schema",
        "observed_at",
        "backend_custody",
        "provider_inventory",
    } or custody.get("schema") != ADAPTER_SCHEMA:
        raise ValueError("lane custody adapter response fields differ")
    observed_at = _fresh(custody["observed_at"], "lane provider custody")

    backend = custody.get("backend_custody")
    expected_managed_addresses = _expected_managed_addresses(manifest)
    backend_fields = {
        "backend_config_sha256",
        "backend_lineage",
        "state_lineage",
        "state_serial",
        "state_version_id",
        "state_snapshot_sha256",
        "managed_addresses",
    }
    if (
        not isinstance(backend, dict)
        or set(backend) != backend_fields
        or not isinstance(backend.get("state_serial"), int)
        or backend["state_serial"] < 1
        or not isinstance(backend.get("state_lineage"), str)
        or not backend["state_lineage"]
        or not isinstance(backend.get("state_version_id"), str)
        or not backend["state_version_id"]
        or backend.get("managed_addresses") != expected_managed_addresses
    ):
        raise ValueError("lane remote-state custody is incomplete")
    for field in ("backend_config_sha256", "backend_lineage", "state_snapshot_sha256"):
        _digest(backend.get(field), f"lane {field}")

    provider = custody.get("provider_inventory")
    if not isinstance(provider, dict) or set(provider) != {
        "authority_project_id",
        "cluster_id",
        "security_group",
        "node_group",
    }:
        raise ValueError("lane live provider inventory is incomplete")
    security_group = provider.get("security_group")
    node_group = provider.get("node_group")
    expected_labels = _expected_labels(generation)
    expected_rules = _expected_rules(generation)
    rules = security_group.get("rules") if isinstance(security_group, dict) else None
    normalized_rules: list[dict[str, Any]] = []
    rule_ids: list[str] = []
    if isinstance(rules, list):
        for rule in rules:
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
                raise ValueError("live lane security-group rule shape differs")
            rule_ids.append(rule["id"])
            normalized_rules.append(
                {key: value for key, value in rule.items() if key != "id"}
            )
    normalized_rules.sort(key=lambda item: item["name"])
    if (
        provider.get("authority_project_id") != generation["authority_project_id"]
        or provider.get("cluster_id") != generation["cluster_id"]
        or not isinstance(security_group, dict)
        or set(security_group) != {"id", "network_id", "labels", "rules"}
        or security_group.get("network_id") != generation["network_id"]
        or security_group.get("labels") != expected_labels
        or len(rule_ids) != len(set(rule_ids))
        or normalized_rules != expected_rules
        or not isinstance(node_group, dict)
        or set(node_group)
        != {
            "id",
            "cluster_id",
            "labels",
            "security_group_ids",
            "min_node_count",
            "max_node_count",
            "strategy",
            "template_labels",
            "template_taints",
            "members",
        }
        or node_group.get("cluster_id") != generation["cluster_id"]
        or node_group.get("labels") != expected_labels
        or node_group.get("security_group_ids") != [security_group.get("id")]
        or node_group.get("min_node_count") != 1
        or node_group.get("max_node_count") != 1
        or node_group.get("strategy")
        != {"max_surge": 0, "max_unavailable": 0, "drain_timeout": "30m"}
        or node_group.get("template_labels")
        != {
            generation["scheduling_key"]: generation["lane_id"],
            "fs2.nebius.ai/provisioning-generation": generation[
                "provisioning_generation"
            ],
        }
        or node_group.get("template_taints")
        != [
            {
                "key": generation["scheduling_key"],
                "value": generation["lane_id"],
                "effect": "NO_SCHEDULE",
            }
        ]
        or generation.get("node_lifecycle_mode")
        != "GENERATIONAL_SINGLETON_RETAIN_PREDECESSOR"
    ):
        raise ValueError("live lane resources differ from the signed provisioning contract")
    members = node_group.get("members")
    if (
        not isinstance(members, list)
        or len(members) != 1
        or any(
            not isinstance(member, dict)
            or set(member) != {"instance_id", "provider_id", "node_group_id"}
            or not isinstance(member.get("instance_id"), str)
            or not member["instance_id"]
            or not isinstance(member.get("provider_id"), str)
            or not member["provider_id"]
            or member.get("node_group_id") != node_group.get("id")
            for member in members
        )
        or members != sorted(members, key=lambda item: item["provider_id"])
        or len({member["provider_id"] for member in members}) != len(members)
    ):
        raise ValueError("live NodeGroup membership is absent or ambiguous")

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "provisioning_generation": args.generation,
        "manifest_sha256": manifest_sha256,
        "authority_project_id": generation["authority_project_id"],
        "cluster_id": generation["cluster_id"],
        "security_group_id": security_group["id"],
        "node_group_id": node_group["id"],
        "backend_custody": backend,
        "backend_custody_sha256": hashlib.sha256(canonical(backend)).hexdigest(),
        "expected_managed_addresses_sha256": hashlib.sha256(
            canonical(expected_managed_addresses)
        ).hexdigest(),
        "provider_inventory": provider,
        "provider_inventory_sha256": hashlib.sha256(canonical(provider)).hexdigest(),
        "network_contract_sha256": hashlib.sha256(
            canonical(
                {
                    "security_group_labels": expected_labels,
                    "security_group_rules": expected_rules,
                    "node_group_labels": expected_labels,
                    "node_group_strategy": {
                        "max_surge": 0,
                        "max_unavailable": 0,
                        "drain_timeout": "30m",
                    },
                }
            )
        ).hexdigest(),
        "daemonset_admission_fence_receipt_sha256": registry[
            "daemonset_admission_fence_receipt_sha256"
        ],
        "node_lifecycle_mode": "GENERATIONAL_SINGLETON_RETAIN_PREDECESSOR",
        "custody_adapter_sha256": adapter_sha256,
        "observed_at": observed_at,
    }
    receipt["payload_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
