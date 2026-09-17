#!/usr/bin/env python3
"""Verify the repository-pinned external IAM and backend custody root.

This verifier intentionally has no command-line overrides for identities, key
digests, backend coordinates, or provider resource IDs.  Those values come
only from the reviewed trust lock committed beside the standalone Terraform
root.  The checked-in lock is inactive, so this revision fails closed until a
separate custodian supplies independently signed provider evidence in a later
reviewed commit.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

LOCK_SCHEMA = "fs2-serve.nebius.ai/sai07-custody-trust-lock/v2"
IAM_SCHEMA = "fs2-serve.nebius.ai/sai07-iam-boundary-receipt/v2"
BACKEND_SCHEMA = "fs2-serve.nebius.ai/sai07-backend-custody-receipt/v2"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_SIZE = 1024 * 1024
MAX_AGE = dt.timedelta(minutes=10)
MAX_LIFETIME = dt.timedelta(minutes=30)
SKEW = dt.timedelta(seconds=30)


class TrustError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TrustError(f"{label} fields differ from the v2 contract")
    return value


def nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TrustError(f"{label} must be a non-empty string")
    return value


def digest(value: object, label: str) -> str:
    value = nonempty(value, label)
    if not SHA256_RE.fullmatch(value):
        raise TrustError(f"{label} must be a lowercase SHA-256")
    return value


def read_regular(path: Path, label: str, limit: int = MAX_SIZE) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise TrustError(f"{label} path must be absolute without parent traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise TrustError(f"{label} is not a bounded regular file")
        payload = b""
        while len(payload) < before.st_size:
            chunk = os.read(descriptor, min(65536, before.st_size - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise TrustError(f"{label} changed during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def load_canonical(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = read_regular(path, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustError(f"{label} is not JSON") from error
    if not isinstance(value, dict) or canonical(value) != payload:
        raise TrustError(f"{label} must be a canonical JSON object")
    return payload, value


def instant(value: object, label: str) -> dt.datetime:
    value = nonempty(value, label)
    if not value.endswith("Z"):
        raise TrustError(f"{label} must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TrustError(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise TrustError(f"{label} must be UTC")
    return parsed


def freshness(receipt: dict[str, Any], label: str) -> None:
    issued = instant(receipt["issued_at"], f"{label}.issued_at")
    expires = instant(receipt["expires_at"], f"{label}.expires_at")
    now = dt.datetime.now(dt.UTC)
    if issued > now + SKEW or now > expires + SKEW:
        raise TrustError(f"{label} is not currently valid")
    if now - issued > MAX_AGE + SKEW or expires - issued > MAX_LIFETIME:
        raise TrustError(f"{label} freshness or lifetime exceeds the contract")


def verify_signature(value: dict[str, Any], key: bytes, key_id: str, label: str) -> None:
    openssl_path = os.environ.get("FS2_SAI07_OPENSSL_PATH")
    if openssl_path != "/proc/1/fd/192":
        raise TrustError(
            "custody receipt verification requires the immutable capsule OpenSSL descriptor"
        )
    signature = exact(value.get("signature"), {"algorithm", "key_id", "value"}, f"{label}.signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != key_id:
        raise TrustError(f"{label} signature authority differs from the trust lock")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise TrustError(f"{label} signature is not strict base64") from error
    if len(raw_signature) != 64:
        raise TrustError(f"{label} signature is not an Ed25519 signature")
    unsigned = dict(value)
    del unsigned["signature"]
    descriptors: list[int] = []
    try:
        for name, payload in (("receipt", canonical(unsigned)), ("signature", raw_signature), ("key", key)):
            descriptor = os.memfd_create(f"fs2-sai07-trust-{name}", flags=0)
            descriptors.append(descriptor)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise TrustError("descriptor-fenced signature input made no progress")
                offset += written
            os.fsync(descriptor)
        completed = subprocess.run(
            [
                openssl_path, "pkeyutl", "-verify", "-pubin", "-inkey",
                f"/proc/self/fd/{descriptors[2]}", "-rawin", "-in",
                f"/proc/self/fd/{descriptors[0]}", "-sigfile",
                f"/proc/self/fd/{descriptors[1]}",
            ],
            check=False,
            capture_output=True,
            timeout=10,
            pass_fds=tuple(descriptors),
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if completed.returncode != 0:
        raise TrustError(f"{label} whole-receipt signature verification failed")


def identity(value: object, label: str) -> dict[str, Any]:
    result = exact(value, {"principal_id", "username", "groups"}, label)
    for field in ("principal_id", "username"):
        nonempty(result[field], f"{label}.{field}")
    groups = result["groups"]
    if not isinstance(groups, list) or not groups or groups != sorted(set(groups)):
        raise TrustError(f"{label}.groups must be a non-empty sorted unique list")
    if not all(isinstance(group, str) and group for group in groups) or "system:masters" in groups:
        raise TrustError(f"{label}.groups contains an unsafe value")
    return result


def validate_iam(value: dict[str, Any], lock: dict[str, Any]) -> None:
    exact(
        value,
        {
            "schema", "issued_at", "expires_at", "provider", "owner", "platform",
            "receipt_operator", "group_exclusions", "policy_evidence", "signature",
        },
        "IAM receipt",
    )
    if value["schema"] != IAM_SCHEMA:
        raise TrustError("IAM receipt schema is unsupported")
    freshness(value, "IAM receipt")
    provider = exact(
        value["provider"],
        {"resource_id", "resource_version", "tenant_id", "project_id"},
        "IAM provider",
    )
    for field in provider:
        nonempty(provider[field], f"IAM provider.{field}")
    owner = identity(value["owner"], "owner")
    platform = identity(value["platform"], "platform")
    receipt = identity(value["receipt_operator"], "receipt operator")
    expected = lock["expected"]
    comparisons = {
        "provider_tenant_id": provider["tenant_id"],
        "provider_project_id": provider["project_id"],
        "owner_principal_id": owner["principal_id"],
        "owner_username": owner["username"],
        "owner_groups": owner["groups"],
        "platform_username": platform["username"],
        "platform_groups": platform["groups"],
        "receipt_username": receipt["username"],
        "receipt_groups": receipt["groups"],
    }
    for field, actual in comparisons.items():
        if expected[field] != actual:
            raise TrustError(f"IAM receipt {field} differs from the reviewed trust lock")
    if platform["principal_id"] not in expected["platform_principal_ids"]:
        raise TrustError("platform principal is absent from the exact exclusion set")
    exclusions = value["group_exclusions"]
    if not isinstance(exclusions, list) or sorted(exclusions) != sorted(expected["platform_principal_ids"]):
        raise TrustError("IAM receipt does not prove the exact platform principal exclusion set")
    evidence = exact(
        value["policy_evidence"],
        {"resource_ids", "resource_versions", "etag", "document_sha256", "observed_at"},
        "IAM policy evidence",
    )
    if not isinstance(evidence["resource_ids"], list) or not evidence["resource_ids"]:
        raise TrustError("IAM policy evidence omits provider resource IDs")
    if not isinstance(evidence["resource_versions"], list) or len(evidence["resource_versions"]) != len(evidence["resource_ids"]):
        raise TrustError("IAM policy evidence omits exact provider versions")
    nonempty(evidence["etag"], "IAM policy evidence.etag")
    digest(evidence["document_sha256"], "IAM policy evidence.document_sha256")
    instant(evidence["observed_at"], "IAM policy evidence.observed_at")


def validate_backend(value: dict[str, Any], lock: dict[str, Any], iam_sha256: str) -> None:
    exact(
        value,
        {"schema", "issued_at", "expires_at", "cluster_id", "kube_system_uid", "iam_receipt_sha256", "backend", "state", "signature"},
        "backend receipt",
    )
    if value["schema"] != BACKEND_SCHEMA:
        raise TrustError("backend receipt schema is unsupported")
    freshness(value, "backend receipt")
    if value["iam_receipt_sha256"] != iam_sha256:
        raise TrustError("backend receipt is not bound to the exact IAM receipt")
    expected = lock["expected"]
    if value["cluster_id"] != expected["cluster_id"] or value["kube_system_uid"] != expected["kube_system_uid"]:
        raise TrustError("backend receipt is bound to another cluster")
    backend = exact(
        value["backend"],
        {
            "type", "bucket", "key", "region", "encrypted", "use_lockfile",
            "backend_resource_id", "lock_resource_id", "retention_policy_id",
            "state_owner_principal_id", "workload_identity_id",
        },
        "backend",
    )
    if backend["type"] != "s3" or backend["encrypted"] is not True or backend["use_lockfile"] is not True:
        raise TrustError("backend is not the exact encrypted lock-enabled S3 contract")
    for field in ("bucket", "key", "region"):
        if backend[field] != lock["backend"][field]:
            raise TrustError(f"backend {field} differs from the reviewed trust lock")
    for field in ("backend_resource_id", "lock_resource_id", "retention_policy_id", "state_owner_principal_id", "workload_identity_id"):
        nonempty(backend[field], f"backend.{field}")
    if backend["state_owner_principal_id"] != expected["owner_principal_id"]:
        raise TrustError("backend state owner is not the reviewed custody owner")
    if backend["workload_identity_id"] in expected["platform_principal_ids"]:
        raise TrustError("a platform principal controls the custody backend workload")
    state = exact(
        value["state"],
        {"lineage", "serial", "object_version", "etag", "state_sha256", "custody_objects_sha256", "custody_object_count", "observed_at"},
        "backend state",
    )
    for field in ("lineage", "object_version", "etag"):
        nonempty(state[field], f"backend state.{field}")
    if not isinstance(state["serial"], int) or isinstance(state["serial"], bool) or state["serial"] < 0:
        raise TrustError("backend state serial is invalid")
    digest(state["state_sha256"], "backend state.state_sha256")
    digest(state["custody_objects_sha256"], "backend state.custody_objects_sha256")
    if not isinstance(state["custody_object_count"], int) or isinstance(state["custody_object_count"], bool) or state["custody_object_count"] < 30:
        raise TrustError("backend state custody object count omits static addresses")
    instant(state["observed_at"], "backend state.observed_at")


def authenticated_owner(query: dict[str, str], expected: dict[str, Any]) -> None:
    base = ["kubectl", "--kubeconfig", query["owner_kubeconfig_path"], "--context", query["owner_context"]]
    body = canonical({"apiVersion": "authentication.k8s.io/v1beta1", "kind": "SelfSubjectReview", "spec": {}})
    completed = subprocess.run(
        [*base, "create", "--raw", "/apis/authentication.k8s.io/v1beta1/selfsubjectreviews", "-f", "-"],
        input=body,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise TrustError("custody owner authentication review failed")
    try:
        user = json.loads(completed.stdout).get("status", {}).get("userInfo", {})
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustError("custody owner authentication response is invalid") from error
    if user.get("username") != expected["owner_username"] or sorted(user.get("groups", [])) != expected["owner_groups"]:
        raise TrustError("authenticated kubeconfig identity differs from the reviewed owner")


def validate(query: dict[str, str]) -> dict[str, str]:
    _, lock = load_canonical(Path(query["trust_lock_path"]), "trust lock")
    exact(lock, {"schema", "activation", "authority_public_key_path", "authority_public_key_sha256", "authority_key_id", "backend", "expected"}, "trust lock")
    if lock["schema"] != LOCK_SCHEMA or lock["activation"] != "active":
        raise TrustError("repository-pinned external custody trust is not active")
    backend_lock = exact(lock["backend"], {"bucket", "key", "region", "use_lockfile", "encrypted"}, "trust lock backend")
    if backend_lock["use_lockfile"] is not True or backend_lock["encrypted"] is not True:
        raise TrustError("trust lock backend safety flags are not enabled")
    expected = exact(
        lock["expected"],
        {
            "cluster_id", "kube_system_uid", "owner_username", "owner_groups",
            "platform_username", "platform_groups", "receipt_username",
            "receipt_groups", "provider_tenant_id", "provider_project_id",
            "owner_principal_id", "platform_principal_ids",
        },
        "trust lock expected identities",
    )
    key_path = Path(nonempty(lock["authority_public_key_path"], "authority_public_key_path"))
    key = read_regular(key_path, "authority public key", 65536)
    if hashlib.sha256(key).hexdigest() != digest(lock["authority_public_key_sha256"], "authority_public_key_sha256"):
        raise TrustError("authority public key differs from the repository-pinned digest")
    key_id = nonempty(lock["authority_key_id"], "authority_key_id")
    iam_bytes, iam = load_canonical(Path(query["iam_boundary_receipt_path"]), "IAM receipt")
    backend_bytes, backend = load_canonical(Path(query["backend_custody_receipt_path"]), "backend receipt")
    verify_signature(iam, key, key_id, "IAM receipt")
    validate_iam(iam, lock)
    iam_sha256 = hashlib.sha256(iam_bytes).hexdigest()
    verify_signature(backend, key, key_id, "backend receipt")
    validate_backend(backend, lock, iam_sha256)
    authenticated_owner(query, expected)
    return {
        "valid": "true",
        "cluster_id": expected["cluster_id"],
        "kube_system_uid": expected["kube_system_uid"],
        "owner_username": expected["owner_username"],
        "owner_groups_json": json.dumps(expected["owner_groups"], separators=(",", ":")),
        "platform_username": expected["platform_username"],
        "platform_groups_json": json.dumps(expected["platform_groups"], separators=(",", ":")),
        "receipt_username": expected["receipt_username"],
        "receipt_groups_json": json.dumps(expected["receipt_groups"], separators=(",", ":")),
        "authority_public_key_path": str(key_path),
        "authority_public_key_sha256": hashlib.sha256(key).hexdigest(),
        "authority_key_id": key_id,
        "iam_receipt_sha256": iam_sha256,
        "backend_receipt_sha256": hashlib.sha256(backend_bytes).hexdigest(),
        "state_lineage": backend["state"]["lineage"],
        "state_serial": str(backend["state"]["serial"]),
        "state_object_version": backend["state"]["object_version"],
        "state_etag": backend["state"]["etag"],
        "state_sha256": backend["state"]["state_sha256"],
        "state_objects_sha256": backend["state"]["custody_objects_sha256"],
        "state_object_count": str(backend["state"]["custody_object_count"]),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {"trust_lock_path", "iam_boundary_receipt_path", "backend_custody_receipt_path", "owner_kubeconfig_path", "owner_context"}
        if not isinstance(query, dict) or set(query) != required or not all(isinstance(query[key], str) for key in required):
            raise TrustError("external query fields differ from the v2 contract")
        result = validate(query)
    except (TrustError, OSError, subprocess.SubprocessError) as error:
        print(f"SAI-07 custody trust rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
