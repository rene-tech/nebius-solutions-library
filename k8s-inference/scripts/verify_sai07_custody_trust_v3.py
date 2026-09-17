#!/usr/bin/env python3
"""Verify independently collected raw SAI-07 provider/backend evidence.

Unlike the rejected v2 receipt-only verifier, this verifier opens the raw
provider artifact, raw backend artifact and exact downloaded platform-state
version.  It digest-matches each file to a distinct signed receipt and invokes
an independent semantic reconstruction before returning any trusted fields.
The checked-in v3 lock is inactive, so this source cannot authorize a rollout.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import sai07_authoritative_evidence as evidence
import verify_sai07_custody_trust as v2

LOCK_SCHEMA = "fs2-serve.nebius.ai/sai07-evidence-collection-contract/v1"
PROVIDER_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/sai07-provider-evidence-receipt/v3"
BACKEND_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/sai07-backend-evidence-receipt/v3"
MAX_KEY_BYTES = 64 * 1024
ROOT = Path(__file__).resolve().parents[1]
TRUST_LOCK = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"


class TrustV3Error(ValueError):
    pass


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise TrustV3Error(f"{label} path must be absolute without traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise TrustV3Error(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise TrustV3Error(f"{label} changed during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def load_json(
    path: Path, label: str, maximum: int, *, repository_document: bool = False
) -> tuple[bytes, dict[str, Any]]:
    payload = read_regular(path, label, maximum)
    document = (
        payload[:-1]
        if repository_document and payload.endswith(b"\n") and not payload.endswith(b"\n\n")
        else payload
    )
    try:
        value = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustV3Error(f"{label} is not JSON") from error
    if not isinstance(value, dict) or evidence.canonical(value) != document:
        raise TrustV3Error(f"{label} must be a canonical JSON object")
    return document, value


def authority(contract: dict[str, Any], name: str) -> tuple[bytes, str, str]:
    value = evidence.exact(
        contract["authorities"][name],
        {"key_id", "principal_id", "public_key_path", "public_key_sha256"},
        f"{name} authority",
    )
    configured_path = Path(evidence.nonempty(value["public_key_path"], f"{name} public-key path"))
    if ".." in configured_path.parts:
        raise TrustV3Error(f"{name} public-key path contains traversal")
    key_path = configured_path if configured_path.is_absolute() else ROOT / configured_path
    key = read_regular(key_path, f"{name} public key", MAX_KEY_BYTES)
    if hashlib.sha256(key).hexdigest() != evidence.sha256(
        value["public_key_sha256"], f"{name} public-key SHA-256"
    ):
        raise TrustV3Error(f"{name} public key differs from the repository-pinned digest")
    return key, evidence.nonempty(value["key_id"], f"{name} key ID"), value["principal_id"]


def validate_receipt(
    receipt: dict[str, Any],
    *,
    schema: str,
    authority_principal: str,
    collection_id: str,
    contract_sha256: str,
    evidence_sha256: str,
    projection_sha256: str,
    label: str,
    additional: dict[str, str],
) -> None:
    expected_fields = {
        "collection_id",
        "contract_sha256",
        "evidence_sha256",
        "expires_at",
        "issued_at",
        "projection_sha256",
        "schema",
        "signature",
        "signer_principal_id",
        *additional,
    }
    evidence.exact(receipt, expected_fields, label)
    if receipt["schema"] != schema:
        raise TrustV3Error(f"{label} schema is unsupported")
    v2.freshness(receipt, label)
    comparisons = {
        "collection_id": collection_id,
        "contract_sha256": contract_sha256,
        "evidence_sha256": evidence_sha256,
        "projection_sha256": projection_sha256,
        "signer_principal_id": authority_principal,
        **additional,
    }
    for field, expected in comparisons.items():
        if receipt[field] != expected:
            raise TrustV3Error(f"{label} {field} differs from independently reconstructed evidence")


def validate(query: dict[str, str]) -> dict[str, str]:
    supplied_lock = Path(query["trust_lock_path"])
    if supplied_lock.resolve() != TRUST_LOCK.resolve():
        raise TrustV3Error("v3 trust verifier requires the repository-pinned contract path")
    contract_bytes, contract = load_json(
        supplied_lock, "v3 trust lock", 1024 * 1024, repository_document=True
    )
    evidence.exact(
        contract,
        {"activation", "authorities", "collector", "expected", "schema", "scope"},
        "v3 trust lock",
    )
    if contract["schema"] != LOCK_SCHEMA or contract["activation"] != "active":
        raise TrustV3Error("repository-pinned authoritative custody trust is not active")
    collector = evidence.exact(
        contract["collector"],
        {
            "max_evidence_bytes",
            "max_pages_per_collection",
            "max_state_bytes",
            "package_versions",
            "page_size",
            "source_path",
            "source_sha256",
        },
        "v3 collector pin",
    )
    source_path = Path(evidence.nonempty(collector["source_path"], "collector source path"))
    if source_path.is_absolute() or ".." in source_path.parts:
        raise TrustV3Error("collector source path must be repository-relative without traversal")
    source = ROOT / source_path
    if hashlib.sha256(read_regular(source, "collector source", 4 * 1024 * 1024)).hexdigest() != evidence.sha256(
        collector["source_sha256"], "collector source SHA-256"
    ):
        raise TrustV3Error("provider-native collector differs from the repository-pinned source")

    provider_bytes, provider_artifact = load_json(
        Path(query["provider_evidence_path"]), "provider evidence", collector["max_evidence_bytes"]
    )
    backend_bytes, backend_artifact = load_json(
        Path(query["backend_evidence_path"]), "backend evidence", collector["max_evidence_bytes"]
    )
    state_bytes = read_regular(
        Path(query["platform_state_path"]), "raw platform state", collector["max_state_bytes"]
    )
    provider_projection = evidence.provider_projection(provider_artifact, contract)
    backend_contract = dict(contract)
    backend_contract["verified_provider_access_keys"] = provider_projection["access_key_owners"]
    backend_projection = evidence.backend_projection(backend_artifact, state_bytes, backend_contract)
    if provider_projection["collection_id"] != backend_projection["collection_id"]:
        raise TrustV3Error("provider and backend evidence use different collection IDs")
    collection_id = provider_projection["collection_id"]
    if provider_artifact["completed_at"] > backend_artifact["started_at"]:
        raise TrustV3Error("backend collection did not follow the complete provider enumeration")

    provider_receipt_bytes, provider_receipt = load_json(
        Path(query["provider_receipt_path"]), "provider evidence receipt", 1024 * 1024
    )
    backend_receipt_bytes, backend_receipt = load_json(
        Path(query["backend_receipt_path"]), "backend evidence receipt", 1024 * 1024
    )
    provider_key, provider_key_id, provider_principal = authority(contract, "provider_receipt")
    backend_key, backend_key_id, backend_principal = authority(contract, "backend_receipt")
    manifest_key, manifest_key_id, manifest_principal = authority(contract, "manifest")
    key_material = {
        hashlib.sha256(provider_key).hexdigest(),
        hashlib.sha256(backend_key).hexdigest(),
        hashlib.sha256(manifest_key).hexdigest(),
    }
    if len(key_material) != 3 or len({provider_principal, backend_principal, manifest_principal}) != 3:
        raise TrustV3Error("provider, backend and manifest authorities are not cryptographically distinct")
    v2.verify_signature(provider_receipt, provider_key, provider_key_id, "provider evidence receipt")
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    validate_receipt(
        provider_receipt,
        schema=PROVIDER_RECEIPT_SCHEMA,
        authority_principal=provider_principal,
        collection_id=collection_id,
        contract_sha256=contract_sha256,
        evidence_sha256=hashlib.sha256(provider_bytes).hexdigest(),
        projection_sha256=provider_projection["projection_sha256"],
        label="provider evidence receipt",
        additional={},
    )
    provider_receipt_sha256 = hashlib.sha256(provider_receipt_bytes).hexdigest()
    v2.verify_signature(backend_receipt, backend_key, backend_key_id, "backend evidence receipt")
    validate_receipt(
        backend_receipt,
        schema=BACKEND_RECEIPT_SCHEMA,
        authority_principal=backend_principal,
        collection_id=collection_id,
        contract_sha256=contract_sha256,
        evidence_sha256=hashlib.sha256(backend_bytes).hexdigest(),
        projection_sha256=backend_projection["projection_sha256"],
        label="backend evidence receipt",
        additional={
            "platform_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "provider_receipt_sha256": provider_receipt_sha256,
        },
    )
    expected = contract["expected"]
    state = backend_projection["projection"]["state"]
    provider_identity = expected["owner"]
    platform_identity = expected["platform"]
    receipt_identity = expected["receipt_operator"]
    return {
        "authority_key_id": manifest_key_id,
        "authority_public_key_path": contract["authorities"]["manifest"]["public_key_path"],
        "authority_public_key_sha256": hashlib.sha256(manifest_key).hexdigest(),
        "backend_evidence_sha256": hashlib.sha256(backend_bytes).hexdigest(),
        "backend_projection_sha256": backend_projection["projection_sha256"],
        "backend_receipt_sha256": hashlib.sha256(backend_receipt_bytes).hexdigest(),
        "cluster_id": expected["cluster_id"],
        "collection_id": collection_id,
        "contract_sha256": contract_sha256,
        "custody_addresses_json": json.dumps(state["custody_addresses"], separators=(",", ":")),
        "iam_receipt_sha256": provider_receipt_sha256,
        "kube_system_uid": expected["kube_system_uid"],
        "owner_groups_json": json.dumps(provider_identity["group_ids"], separators=(",", ":")),
        "owner_username": provider_identity["username"],
        "platform_groups_json": json.dumps(platform_identity["group_ids"], separators=(",", ":")),
        "platform_username": platform_identity["username"],
        "provider_completed_at": provider_artifact["completed_at"],
        "provider_evidence_sha256": hashlib.sha256(provider_bytes).hexdigest(),
        "provider_projection_sha256": provider_projection["projection_sha256"],
        "receipt_groups_json": json.dumps(receipt_identity["group_ids"], separators=(",", ":")),
        "receipt_username": receipt_identity["username"],
        "state_addresses_sha256": state["custody_addresses_sha256"],
        "state_etag": state["etag"],
        "state_lineage": state["lineage"],
        "state_object_count": str(state["custody_object_count"]),
        "state_object_version": state["version_id"],
        "state_serial": str(state["serial"]),
        "state_sha256": state["sha256"],
        "backend_completed_at": backend_artifact["completed_at"],
        "valid": "true",
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "backend_evidence_path",
            "backend_receipt_path",
            "platform_state_path",
            "provider_evidence_path",
            "provider_receipt_path",
            "trust_lock_path",
        }
        if (
            not isinstance(query, dict)
            or set(query) != required
            or not all(isinstance(query[field], str) for field in required)
        ):
            raise TrustV3Error("external query fields differ from the v3 authoritative contract")
        result = validate(query)
    except (TrustV3Error, evidence.EvidenceError, v2.TrustError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SAI-07 authoritative custody trust rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
