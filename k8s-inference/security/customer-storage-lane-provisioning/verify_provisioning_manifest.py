#!/usr/bin/env python3
"""Verify a provider-only signed stable-lane provisioning manifest."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REGISTRY = Path("/etc/fs2-security-ro/authority/customer-storage-lane-provisioning.json")
MAX_BYTES = 1024 * 1024
SCHEMA = "fs2-serve.nebius.ai/protected-lane-provisioning/v3"
FIELDS = {
    "provisioning_generation",
    "lane_id",
    "scheduling_key",
    "authority_project_id",
    "cluster_id",
    "network_id",
    "subnet_id",
    "node_service_account_id",
    "kubernetes_version",
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
    "node_lifecycle_mode",
}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def safe_root_read(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
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
            raise ValueError("provisioning registry is not a root-owned read-only regular file")
        payload = b""
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("provisioning registry changed during descriptor-bound read")
        return payload
    finally:
        os.close(descriptor)


def strict_json(payload: bytes | str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def verify_signed(manifest: dict[str, object], public_key_pem: str) -> str:
    body = {key: value for key, value in manifest.items() if key not in {"payload_sha256", "signature"}}
    digest = hashlib.sha256(canonical(body)).hexdigest()
    if manifest.get("payload_sha256") != digest:
        raise ValueError("provisioning manifest digest differs")
    key = serialization.load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("provisioning key must be Ed25519")
    try:
        key.verify(base64.b64decode(str(manifest["signature"]), validate=True), canonical(body))
    except (InvalidSignature, TypeError, ValueError, KeyError) as exc:
        raise ValueError("provisioning manifest signature is invalid") from exc
    return digest


def exact_host_routes(value: object, label: str) -> None:
    if not isinstance(value, list) or not value or value != sorted(set(value)):
        raise ValueError(f"{label} must be a non-empty sorted unique list")
    for raw in value:
        network = ipaddress.ip_network(str(raw), strict=True)
        if network.prefixlen != network.max_prefixlen:
            raise ValueError(f"{label} accepts exact host routes only")


def private_routes(value: object) -> None:
    if not isinstance(value, list) or not value or value != sorted(set(value)):
        raise ValueError("private CIDRs must be a non-empty sorted unique list")
    if any(not ipaddress.ip_network(str(raw), strict=True).is_private for raw in value):
        raise ValueError("private CIDRs must be private networks")


def main() -> None:
    query = strict_json(sys.stdin.read())
    if set(query) != {"manifest_json"}:
        raise ValueError("provisioning verifier query fields differ")
    registry = strict_json(safe_root_read(REGISTRY))
    if set(registry) != {
        "schema",
        "approved_manifest_sha256",
        "manifest_public_key_pem",
        "checkpoint_public_key_pem",
        "custody_adapter_sha256",
        "daemonset_admission_fence_receipt_sha256",
    } or registry.get("schema") != "fs2-serve.nebius.ai/protected-lane-provisioning-registry/v3":
        raise ValueError("provisioning registry differs")
    if not re.fullmatch(r"[a-f0-9]{64}", str(registry["custody_adapter_sha256"])):
        raise ValueError("provisioning custody adapter digest is invalid")
    if not re.fullmatch(
        r"[a-f0-9]{64}",
        str(registry["daemonset_admission_fence_receipt_sha256"]),
    ):
        raise ValueError("continuous DaemonSet fence receipt digest is invalid")
    manifest = strict_json(str(query["manifest_json"]))
    if set(manifest) != {
        "schema",
        "generations",
        "current_generation",
        "payload_sha256",
        "signature",
    } or manifest.get("schema") != SCHEMA:
        raise ValueError("provisioning manifest fields differ")
    generations = manifest.get("generations")
    if not isinstance(generations, list) or not generations:
        raise ValueError("provisioning manifest needs retained and current generations")
    normalized: dict[str, dict[str, object]] = {}
    lane_ids: set[str] = set()
    for generation in generations:
        if not isinstance(generation, dict) or set(generation) != FIELDS:
            raise ValueError("stable lane generation fields differ")
        content_digest = hashlib.sha256(
            canonical(
                {
                    key: generation[key]
                    for key in sorted(FIELDS - {"provisioning_generation"})
                }
            )
        ).hexdigest()
        provisioning_id = generation.get("provisioning_generation")
        lane_id = generation.get("lane_id")
        if (
            not isinstance(provisioning_id, str)
            or not re.fullmatch(r"p[0-9]{14}-[a-f0-9]{12}", provisioning_id)
            or provisioning_id[-12:] != content_digest[:12]
            or provisioning_id in normalized
            or not isinstance(lane_id, str)
            or not re.fullmatch(r"l[0-9]{14}-[a-f0-9]{12}", lane_id)
            or lane_id in lane_ids
            or generation.get("scheduling_key")
            != f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
            # Bootstrap is deliberately one node.  A DaemonSet cannot cause a
            # zero-sized NodeGroup to scale, and Node attestation is a later
            # phase that cannot authorize the NodeGroup which creates it.
            or generation.get("min_node_count") != 1
            or generation.get("max_node_count") != 1
            or generation.get("node_lifecycle_mode")
            != "PARALLEL_GENERATIONAL_SINGLETON_CUTOVER_RETAIN_PREDECESSOR"
            or any(
                not isinstance(generation.get(field), str) or not generation[field]
                for field in (
                    "authority_project_id",
                    "cluster_id",
                    "network_id",
                    "subnet_id",
                    "node_service_account_id",
                    "kubernetes_version",
                    "platform",
                    "preset",
                )
            )
            or generation.get("boot_disk_type")
            not in {"NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED"}
            or not isinstance(generation.get("boot_disk_gib"), int)
            or not 32 <= generation["boot_disk_gib"] <= 256
        ):
            raise ValueError("stable lane identity is not content-bound and unique")
        exact_host_routes(generation.get("provider_api_cidrs"), "provider API CIDRs")
        exact_host_routes(generation.get("kubernetes_api_cidrs"), "Kubernetes API CIDRs")
        exact_host_routes(generation.get("bootstrap_https_cidrs"), "bootstrap HTTPS CIDRs")
        private_routes(generation.get("private_cidrs"))
        normalized[provisioning_id] = generation
        lane_ids.add(lane_id)
    if manifest.get("current_generation") != list(normalized)[-1]:
        raise ValueError("current provisioning generation must be the final retained entry")
    manifest_sha256 = verify_signed(manifest, str(registry["manifest_public_key_pem"]))
    if manifest_sha256 != registry.get("approved_manifest_sha256"):
        raise ValueError("provisioning manifest is not externally approved")
    print(
        json.dumps(
            {
                "authorized": "true",
                "manifest_sha256": manifest_sha256,
                "generations_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                "current_generation": str(manifest["current_generation"]),
                "daemonset_admission_fence_receipt_sha256": str(
                    registry["daemonset_admission_fence_receipt_sha256"]
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
