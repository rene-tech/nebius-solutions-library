#!/usr/bin/env python3
"""Offline verifier for an independently issued SAI-07 custody handoff.

This program is intentionally unable to contact Kubernetes.  The platform
Terraform root gives it only a signed handoff, a reviewed public key, and the
hashes of the exact rollout context and receipt bundle.  Live reads, TokenRequest
creation, authority review, ledger CAS, and post-apply acknowledgement belong to
the separately administered ``stages/pod-security-custody`` pipeline.
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

SCHEMA = "fs2-serve.nebius.ai/sai07-external-custody-handoff/v1"
SECRET_METADATA_SCHEMA = "fs2-serve.nebius.ai/sai07-secret-metadata/v1"
PARTIAL_METADATA_ACCEPT = (
    "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
)
API_AUDIENCE = "https://kubernetes.default.svc"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
DNS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
MAX_HANDOFF_AGE = dt.timedelta(minutes=2)
MAX_HANDOFF_LIFETIME = dt.timedelta(minutes=10)
MAX_CLOCK_SKEW = dt.timedelta(seconds=30)
MAX_FILE_SIZE = 2 * 1024 * 1024

# These denials close the known RBAC/impersonation pivots.  The signed audit
# may include more checks, but it may not omit any of these exact checks.
REQUIRED_DENIALS = frozenset(
    {
        "impersonate:authentication.k8s.io/users",
        "impersonate:authentication.k8s.io/groups",
        "impersonate:v1/serviceaccounts",
        "create:authentication.k8s.io/tokenreviews",
        "create:authorization.k8s.io/subjectaccessreviews",
        "bind:rbac.authorization.k8s.io/roles",
        "bind:rbac.authorization.k8s.io/clusterroles",
        "escalate:rbac.authorization.k8s.io/roles",
        "escalate:rbac.authorization.k8s.io/clusterroles",
        "create:rbac.authorization.k8s.io/rolebindings",
        "update:rbac.authorization.k8s.io/rolebindings",
        "patch:rbac.authorization.k8s.io/rolebindings",
        "create:rbac.authorization.k8s.io/clusterrolebindings",
        "update:rbac.authorization.k8s.io/clusterrolebindings",
        "patch:rbac.authorization.k8s.io/clusterrolebindings",
        "create:admissionregistration.k8s.io/validatingadmissionpolicies",
        "update:admissionregistration.k8s.io/validatingadmissionpolicies",
        "patch:admissionregistration.k8s.io/validatingadmissionpolicies",
        "delete:admissionregistration.k8s.io/validatingadmissionpolicies",
        "create:admissionregistration.k8s.io/validatingadmissionpolicybindings",
        "update:admissionregistration.k8s.io/validatingadmissionpolicybindings",
        "patch:admissionregistration.k8s.io/validatingadmissionpolicybindings",
        "delete:admissionregistration.k8s.io/validatingadmissionpolicybindings",
        "get:v1/secrets",
        "list:v1/secrets",
        "watch:v1/secrets",
        "create:v1/serviceaccounts/token",
        "update:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger",
        "patch:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger",
        "delete:v1/configmaps/fs2-system/fs2-pod-security-rollout-ledger",
        "create:v1/pods/proxy",
        "get:v1/pods/proxy",
        "create:v1/services/proxy",
        "get:v1/services/proxy",
    }
)


class HandoffError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise HandoffError(f"{label} fields differ from the v1 contract")
    return value


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HandoffError(f"{label} must be a non-empty string")
    return value


def sha256(value: object, label: str) -> str:
    value = text(value, label)
    if not SHA256_RE.fullmatch(value):
        raise HandoffError(f"{label} must be a lowercase SHA-256")
    return value


def instant(value: object, label: str) -> dt.datetime:
    value = text(value, label)
    if not value.endswith("Z"):
        raise HandoffError(f"{label} must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise HandoffError(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise HandoffError(f"{label} must use UTC")
    return parsed


def read_regular(path: Path, label: str, limit: int = MAX_FILE_SIZE) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise HandoffError(f"{label} path must be absolute without parent traversal")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoffError(f"cannot safely open {label}: {error.strerror}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise HandoffError(f"{label} is not a bounded regular file")
        payload = b""
        while len(payload) < before.st_size:
            chunk = os.read(descriptor, min(65536, before.st_size - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise HandoffError(f"{label} changed during its descriptor-fenced read")
        if len(payload) != before.st_size:
            raise HandoffError(f"{label} was truncated during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def verify_signature(bundle: dict[str, Any], key: bytes, expected_key_id: str) -> None:
    signature = exact_keys(bundle.get("signature"), {"algorithm", "key_id", "value"}, "signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != expected_key_id:
        raise HandoffError("handoff signature authority differs")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise HandoffError("handoff signature is not strict base64") from error
    if len(raw_signature) != 64:
        raise HandoffError("Ed25519 signature must be exactly 64 bytes")
    unsigned = dict(bundle)
    del unsigned["signature"]
    descriptors: list[int] = []
    try:
        for label, payload in (("handoff", canonical(unsigned)), ("signature", raw_signature), ("key", key)):
            descriptor = os.memfd_create(f"fs2-sai07-{label}", flags=0)
            descriptors.append(descriptor)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise HandoffError("descriptor-fenced signature write made no progress")
                offset += written
            os.fsync(descriptor)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{descriptors[2]}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{descriptors[0]}",
                "-sigfile",
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
        raise HandoffError("whole-handoff signature verification failed")


def validate_audit(value: object, label: str, query: dict[str, str]) -> None:
    audit = exact_keys(
        value,
        {
            "schema",
            "username",
            "groups",
            "cluster_id",
            "kube_system_uid",
            "namespace_inventory_sha256",
            "namespace_count",
            "self_subject_rules_review_sha256",
            "self_subject_access_review_sha256",
            "denied_checks",
            "observed_at",
        },
        label,
    )
    username = text(audit["username"], f"{label}.username")
    if audit["schema"] != "fs2-serve.nebius.ai/sai07-effective-authority-audit/v1":
        raise HandoffError(f"{label} schema is unsupported")
    groups = audit["groups"]
    if (
        username.startswith("system:")
        or not isinstance(groups, list)
        or groups != sorted(set(groups))
        or "system:masters" in groups
    ):
        raise HandoffError(f"{label} identity is not a bounded external identity")
    sha256(audit["namespace_inventory_sha256"], f"{label}.namespace_inventory_sha256")
    sha256(audit["self_subject_rules_review_sha256"], f"{label}.self_subject_rules_review_sha256")
    sha256(
        audit["self_subject_access_review_sha256"],
        f"{label}.self_subject_access_review_sha256",
    )
    if not isinstance(audit["namespace_count"], int) or audit["namespace_count"] < 1:
        raise HandoffError(f"{label}.namespace_count must cover the non-empty exact cluster inventory")
    denials = audit["denied_checks"]
    if not isinstance(denials, list) or not all(isinstance(item, str) for item in denials):
        raise HandoffError(f"{label}.denied_checks is malformed")
    if not REQUIRED_DENIALS.issubset(denials):
        raise HandoffError(f"{label} omits required effective-authority denials")
    observed_at = instant(audit["observed_at"], f"{label}.observed_at")
    if dt.datetime.now(dt.UTC) - observed_at > MAX_HANDOFF_AGE + MAX_CLOCK_SKEW:
        raise HandoffError(f"{label} is stale")
    if audit["cluster_id"] != query["cluster_id"] or audit["kube_system_uid"] != query["kube_system_uid"]:
        raise HandoffError(f"{label} is bound to another cluster")


def validate_bound_token(value: object, label: str, expected_name: str) -> dict[str, Any]:
    token = exact_keys(
        value,
        {"audiences", "expiration_seconds", "service_account", "bound_object_ref", "jti_sha256"},
        label,
    )
    if (
        token["audiences"] != [API_AUDIENCE]
        or not isinstance(token["expiration_seconds"], int)
        or isinstance(token["expiration_seconds"], bool)
        or not 1 <= token["expiration_seconds"] <= 600
    ):
        raise HandoffError(f"{label} audience or expiration is outside the exact contract")
    service_account = exact_keys(
        token["service_account"], {"namespace", "name", "uid"}, f"{label} service account"
    )
    bound = exact_keys(
        token["bound_object_ref"],
        {"api_version", "kind", "namespace", "name", "uid"},
        f"{label} boundObjectRef",
    )
    if (
        service_account["namespace"] != "fs2-system"
        or service_account["name"] != expected_name
        or not text(service_account["uid"], f"{label}.service_account.uid")
    ):
        raise HandoffError(f"{label} is not bound to the exact service account")
    if (
        bound["api_version"] != "v1"
        or bound["kind"] != "Secret"
        or bound["namespace"] != "fs2-system"
        or bound["name"] != "fs2-pod-security-token-anchor"
        or not text(bound["uid"], f"{label}.bound_object_ref.uid")
    ):
        raise HandoffError(f"{label} is not bound to the exact external custody token anchor")
    sha256(token["jti_sha256"], f"{label}.jti_sha256")
    return token


def validate(bundle: dict[str, Any], query: dict[str, str]) -> dict[str, str]:
    exact_keys(
        bundle,
        {
            "schema",
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "context_sha256",
            "phase",
            "consumer",
            "action",
            "bundle_sha256",
            "issued_at",
            "expires_at",
            "nonce",
            "custody",
            "receipt_token_request",
            "metadata_token_request",
            "platform_authority_audit",
            "owner_authority_audit",
            "secret_metadata_inventory",
            "adopted_objects",
            "ledger",
            "signature",
        },
        "handoff",
    )
    if bundle["schema"] != SCHEMA:
        raise HandoffError("handoff schema is unsupported")
    for field in ("cluster_id", "run_id", "kube_system_uid", "nonce"):
        text(bundle[field], field)
    for field in ("context_sha256", "bundle_sha256"):
        sha256(bundle[field], field)
    for field in ("phase", "consumer", "action", "bundle_sha256", "context_sha256"):
        expected = query[f"expected_{field}"] if field != "bundle_sha256" else query["receipt_bundle_sha256"]
        if bundle[field] != expected:
            raise HandoffError(f"handoff {field} differs from the platform invocation")
    if bundle["cluster_id"] != query["cluster_id"] or bundle["kube_system_uid"] != query["kube_system_uid"]:
        raise HandoffError("handoff cluster identity differs")

    issued_at = instant(bundle["issued_at"], "issued_at")
    expires_at = instant(bundle["expires_at"], "expires_at")
    now = dt.datetime.now(dt.UTC)
    if issued_at > now + MAX_CLOCK_SKEW or now > expires_at + MAX_CLOCK_SKEW:
        raise HandoffError("handoff is not currently valid")
    if now - issued_at > MAX_HANDOFF_AGE + MAX_CLOCK_SKEW or expires_at - issued_at > MAX_HANDOFF_LIFETIME:
        raise HandoffError("handoff freshness or lifetime exceeds the contract")

    custody = exact_keys(
        bundle["custody"],
        {"owner_username", "owner_groups", "receipt_username", "receipt_groups", "iam_boundary_sha256"},
        "custody",
    )
    identities = [text(custody[name], f"custody.{name}") for name in ("owner_username", "receipt_username")]
    if identities[0] == identities[1] or any(value.startswith("system:") for value in identities):
        raise HandoffError("custody owner and receipt identities must be distinct external subjects")
    for field in ("owner_groups", "receipt_groups"):
        groups = custody[field]
        if not isinstance(groups, list) or groups != sorted(set(groups)) or "system:masters" in groups:
            raise HandoffError(f"custody.{field} is not a bounded group set")
    if custody["owner_groups"] != ["fs2-pod-security-custody-owners"] or custody[
        "receipt_groups"
    ] != ["fs2-pod-security-receipt-custodians"]:
        raise HandoffError("custody groups differ from the external trust boundary")
    sha256(custody["iam_boundary_sha256"], "custody.iam_boundary_sha256")

    receipt_token = validate_bound_token(
        bundle["receipt_token_request"],
        "receipt_token_request",
        "fs2-pod-security-rollout-custodian",
    )
    metadata_token = validate_bound_token(
        bundle["metadata_token_request"],
        "metadata_token_request",
        "fs2-pod-security-metadata-reader",
    )
    if (
        receipt_token["jti_sha256"] == metadata_token["jti_sha256"]
        or receipt_token["bound_object_ref"] != metadata_token["bound_object_ref"]
    ):
        raise HandoffError("receipt and metadata tokens must be distinct and bound to one anchor")

    validate_audit(bundle["platform_authority_audit"], "platform_authority_audit", query)
    validate_audit(bundle["owner_authority_audit"], "owner_authority_audit", query)
    if (
        bundle["platform_authority_audit"]["username"]
        == bundle["owner_authority_audit"]["username"]
        or bundle["platform_authority_audit"]["namespace_inventory_sha256"]
        != bundle["owner_authority_audit"]["namespace_inventory_sha256"]
        or bundle["platform_authority_audit"]["namespace_count"]
        != bundle["owner_authority_audit"]["namespace_count"]
    ):
        raise HandoffError(
            "platform/owner audits must cover distinct identities and the same exact namespace inventory"
        )

    metadata = exact_keys(
        bundle["secret_metadata_inventory"],
        {
            "schema",
            "media_type",
            "namespace",
            "collection_resource_version",
            "artifact_sha256",
            "items_sha256",
            "item_count",
            "contains_secret_payload",
            "reader_service_account_uid",
            "token_jti_sha256",
        },
        "secret_metadata_inventory",
    )
    if (
        metadata["schema"] != SECRET_METADATA_SCHEMA
        or metadata["media_type"] != PARTIAL_METADATA_ACCEPT
        or metadata["namespace"] != "fs2-models"
        or metadata["contains_secret_payload"] is not False
        or not isinstance(metadata["item_count"], int)
        or metadata["item_count"] < 0
    ):
        raise HandoffError("Secret inventory is not a true metadata-only collection")
    for field in ("artifact_sha256", "items_sha256", "token_jti_sha256"):
        sha256(metadata[field], f"secret_metadata_inventory.{field}")
    if (
        metadata["token_jti_sha256"] != metadata_token["jti_sha256"]
        or metadata["reader_service_account_uid"]
        != metadata_token["service_account"]["uid"]
    ):
        raise HandoffError("Secret metadata inventory is not bound to the short-lived reader token")
    text(metadata["collection_resource_version"], "secret_metadata_inventory.collection_resource_version")

    adopted = exact_keys(bundle["adopted_objects"], {"schema", "artifact_sha256", "count"}, "adopted_objects")
    if adopted["schema"] != "fs2-serve.nebius.ai/sai07-custody-adoption/v1" or not isinstance(adopted["count"], int) or adopted["count"] < 1:
        raise HandoffError("external custody adoption evidence is incomplete")
    sha256(adopted["artifact_sha256"], "adopted_objects.artifact_sha256")

    ledger = exact_keys(
        bundle["ledger"], {"namespace", "name", "uid", "resource_version", "sequence", "state", "nonce"}, "ledger"
    )
    if ledger["namespace"] != "fs2-system" or ledger["name"] != "fs2-pod-security-rollout-ledger":
        raise HandoffError("handoff binds an unexpected ledger")
    if not isinstance(ledger["sequence"], int) or ledger["sequence"] < 1 or ledger["nonce"] != bundle["nonce"]:
        raise HandoffError("handoff ledger sequence or nonce is invalid")
    for field in ("uid", "resource_version", "state"):
        text(ledger[field], f"ledger.{field}")

    return {
        "valid": "true",
        "phase": bundle["phase"],
        "consumer": bundle["consumer"],
        "action": bundle["action"],
        "bundle_sha256": bundle["bundle_sha256"],
        "terminal_state": ledger["state"],
        "sequence": str(ledger["sequence"]),
        "handoff_sha256": hashlib.sha256(canonical(bundle)).hexdigest(),
        "ledger_uid": ledger["uid"],
        "ledger_resource_version": ledger["resource_version"],
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "handoff_path",
            "handoff_public_key_path",
            "handoff_public_key_sha256",
            "handoff_key_id",
            "receipt_bundle_sha256",
            "expected_context_sha256",
            "expected_phase",
            "expected_consumer",
            "expected_action",
            "cluster_id",
            "kube_system_uid",
        }
        if not isinstance(query, dict) or set(query) != required or not all(isinstance(query[key], str) for key in required):
            raise HandoffError("external query fields differ from the v1 contract")
        handoff_bytes = read_regular(Path(query["handoff_path"]), "handoff")
        key_bytes = read_regular(Path(query["handoff_public_key_path"]), "handoff public key", 65536)
        if hashlib.sha256(key_bytes).hexdigest() != query["handoff_public_key_sha256"]:
            raise HandoffError("handoff public key digest differs from the reviewed digest")
        try:
            bundle = json.loads(handoff_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HandoffError("handoff is not canonical JSON") from error
        if canonical(bundle) != handoff_bytes:
            raise HandoffError("handoff bytes are not canonical JSON")
        verify_signature(bundle, key_bytes, query["handoff_key_id"])
        result = validate(bundle, query)
    except (HandoffError, OSError, subprocess.SubprocessError) as error:
        print(f"SAI-07 external handoff rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
