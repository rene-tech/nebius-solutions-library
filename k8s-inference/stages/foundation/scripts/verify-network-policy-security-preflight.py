#!/usr/bin/env python3
"""Plan-time proof for the external public-edge security boundary."""

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
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROVIDER_ADAPTER_ID = "fs2-serve.nebius.ai/nebius-iam-human-directory/v2"
PROVIDER_TRUST_ANCHOR_PATH = Path("/etc/fs2/security/network-policy-provider-trust-anchor-v2.json")


class PreflightError(RuntimeError):
    """The plan-time security boundary is not exact."""


def descriptor_bytes(path: Path, *, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    raise PreflightError(f"{label} exceeds its size bound")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise PreflightError(f"{label} cannot be opened safely") from error
    return b"".join(chunks), metadata


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def decode_base64url(value: Any, *, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PreflightError("security inventory contains invalid base64url data")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise PreflightError("security inventory contains invalid base64url data") from error
    if len(decoded) != size:
        raise PreflightError("security inventory cryptographic length is invalid")
    return decoded


def verified_envelope(raw_value: str, public_key: str, *, label: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise PreflightError(f"{label} is not valid JSON") from error
    if not isinstance(envelope, dict) or set(envelope) != {"signed", "signature"}:
        raise PreflightError(f"{label} envelope is not exact")
    signed = envelope.get("signed")
    if not isinstance(signed, dict):
        raise PreflightError(f"{label} signed payload is not an object")
    key_id = hashlib.sha256(public_key.encode()).hexdigest()
    try:
        Ed25519PublicKey.from_public_bytes(decode_base64url(public_key, size=32)).verify(
            decode_base64url(envelope.get("signature"), size=64),
            canonical(signed).encode(),
        )
    except InvalidSignature as error:
        raise PreflightError(f"{label} signature is invalid") from error
    if signed.get("signer_key_id") != key_id:
        raise PreflightError(f"{label} signer key is not exact")
    return signed


def normalized_subjects(
    signed: dict[str, Any], *, forbidden_usernames: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    users = signed.get("human_users")
    groups = signed.get("human_groups")
    if not isinstance(users, list) or not users or not isinstance(groups, list) or not groups:
        raise PreflightError("security subject set is empty or malformed")
    if len(groups) != len(set(groups)) or any(
        not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group) for group in groups
    ):
        raise PreflightError("security subject group inventory is invalid")
    normalized_users: list[dict[str, Any]] = []
    for subject in users:
        if (
            not isinstance(subject, dict)
            or set(subject) != {"username", "groups"}
            or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{3,253}", str(subject.get("username", "")))
            or subject.get("username") in forbidden_usernames
            or not isinstance(subject.get("groups"), list)
            or len(subject["groups"]) != len(set(subject["groups"]))
            or any(
                not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group)
                for group in subject["groups"]
            )
        ):
            raise PreflightError("security subject user inventory is invalid")
        normalized_users.append({"username": subject["username"], "groups": sorted(subject["groups"])})
    normalized_users.sort(key=lambda value: value["username"])
    if len({subject["username"] for subject in normalized_users}) != len(normalized_users):
        raise PreflightError("security subject user inventory contains duplicates")
    return normalized_users, sorted(groups)


def verified_provider_trust_anchor(
    path: Path,
    adapter_path: Path,
    *,
    rollback_valid_until: int,
) -> tuple[dict[str, Any], str, str]:
    if path != PROVIDER_TRUST_ANCHOR_PATH:
        raise PreflightError("provider trust anchor path is not source-fixed")
    expected_adapter = Path(__file__).resolve().parents[3] / (
        "components/control-plane/scripts/network_policy_subject_provider_adapter.py"
    )
    if adapter_path.resolve() != expected_adapter:
        raise PreflightError("provider adapter path is not source-fixed")
    try:
        trust_bytes, trust_metadata = descriptor_bytes(path, maximum=65536, label="provider trust anchor")
        adapter_bytes, adapter_metadata = descriptor_bytes(
            adapter_path,
            maximum=1048576,
            label="provider adapter",
        )
        trust = json.loads(trust_bytes.decode("utf-8"))
        adapter_sha256 = hashlib.sha256(adapter_bytes).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("provider trust custody is unavailable") from error
    expected_fields = {
        "schema",
        "provider",
        "adapter",
        "directory_query",
        "tenant_sha256",
        "query_sha256",
        "snapshot_public_key",
        "snapshot_signer_key_id",
        "valid_from",
        "expires_at",
    }
    now = dt.datetime.now(dt.UTC)
    valid_from = dt.datetime.fromisoformat(str(trust.get("valid_from", "")).replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(str(trust.get("expires_at", "")).replace("Z", "+00:00"))
    public_key = trust.get("snapshot_public_key")
    directory_query = trust.get("directory_query", {})
    if (
        not stat.S_ISREG(trust_metadata.st_mode)
        or trust_metadata.st_uid != 0
        or trust_metadata.st_gid != 0
        or stat.S_IMODE(trust_metadata.st_mode) not in {0o400, 0o444}
        or not stat.S_ISREG(adapter_metadata.st_mode)
        or set(trust) != expected_fields
        or trust.get("schema") != "fs2-serve.nebius.ai/security-provider-trust-anchor/v2"
        or trust.get("provider") != "nebius-iam"
        or trust.get("adapter") != {"id": PROVIDER_ADAPTER_ID, "sha256": adapter_sha256}
        or not isinstance(trust.get("directory_query"), dict)
        or set(directory_query)
        != {
            "cli_path",
            "config_path",
            "profile",
            "tenant_id",
            "page_size",
            "max_pages",
            "max_records",
            "timeout_seconds",
            "snapshot_ttl_seconds",
        }
        or directory_query.get("cli_path") != "/usr/local/bin/nebius"
        or directory_query.get("config_path") != "/etc/fs2/security/nebius-directory-reader.yaml"
        or not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", str(directory_query.get("profile", "")))
        or not re.fullmatch(r"tenant-[A-Za-z0-9-]{8,128}", str(directory_query.get("tenant_id", "")))
        or not isinstance(directory_query.get("page_size"), int)
        or not 1 <= directory_query["page_size"] <= 1000
        or not isinstance(directory_query.get("max_pages"), int)
        or not 1 <= directory_query["max_pages"] <= 10000
        or not isinstance(directory_query.get("max_records"), int)
        or not 1 <= directory_query["max_records"] <= 100000
        or not isinstance(directory_query.get("timeout_seconds"), int)
        or not 1 <= directory_query["timeout_seconds"] <= 120
        or not isinstance(directory_query.get("snapshot_ttl_seconds"), int)
        or not 300 <= directory_query["snapshot_ttl_seconds"] <= 3600
        or trust.get("tenant_sha256")
        != hashlib.sha256(str(trust["directory_query"].get("tenant_id", "")).encode()).hexdigest()
        or trust.get("query_sha256")
        != hashlib.sha256(canonical(trust["directory_query"]).encode()).hexdigest()
        or not re.fullmatch(r"[0-9a-f]{64}", str(trust.get("tenant_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(trust.get("query_sha256", "")))
        or not isinstance(public_key, str)
        or trust.get("snapshot_signer_key_id") != hashlib.sha256(public_key.encode()).hexdigest()
        or valid_from.tzinfo is None
        or expires.tzinfo is None
        or valid_from.astimezone(dt.UTC) > now
        or expires.astimezone(dt.UTC) <= now
        or int(expires.timestamp()) < rollback_valid_until
    ):
        raise PreflightError("provider trust anchor is not exact, root-owned or rollback-valid")
    decode_base64url(public_key, size=32)
    return trust, hashlib.sha256(canonical(trust).encode()).hexdigest(), adapter_sha256


def verified_provider_snapshot(
    raw_snapshot: str,
    trust: dict[str, Any],
    *,
    trust_anchor_sha256: str,
    adapter_sha256: str,
    rollback_valid_until: int,
    forbidden_usernames: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], str]:
    provider_public_key = trust["snapshot_public_key"]
    signed = verified_envelope(raw_snapshot, provider_public_key, label="provider/IAM subject snapshot")
    expected_fields = {
        "schema",
        "snapshot_id",
        "provider",
        "adapter",
        "trust_anchor_sha256",
        "tenant_sha256",
        "query_sha256",
        "complete",
        "pagination",
        "human_users",
        "human_groups",
        "captured_at",
        "expires_at",
        "signer_key_id",
    }
    if set(signed) != expected_fields:
        raise PreflightError("provider/IAM subject snapshot fields are not exact")
    now = dt.datetime.now(dt.UTC)
    captured = dt.datetime.fromisoformat(str(signed.get("captured_at", "")).replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(str(signed.get("expires_at", "")).replace("Z", "+00:00"))
    if (
        signed.get("schema") != "fs2-serve.nebius.ai/security-subject-provider-snapshot/v2"
        or signed.get("complete") is not True
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(signed.get("snapshot_id", "")))
        or signed.get("provider") != trust["provider"]
        or signed.get("adapter") != {"id": PROVIDER_ADAPTER_ID, "sha256": adapter_sha256}
        or signed.get("trust_anchor_sha256") != trust_anchor_sha256
        or signed.get("tenant_sha256") != trust["tenant_sha256"]
        or signed.get("query_sha256") != trust["query_sha256"]
        or captured.tzinfo is None
        or expires.tzinfo is None
        or captured.astimezone(dt.UTC) > now
        or expires.astimezone(dt.UTC) <= now
        or int(expires.timestamp()) < rollback_valid_until
    ):
        raise PreflightError("provider/IAM subject snapshot is incomplete, stale or from another tenant/query")
    users, groups = normalized_subjects(signed, forbidden_usernames=forbidden_usernames)
    pagination = signed.get("pagination")
    if not isinstance(pagination, dict) or set(pagination) != {
        "page_size",
        "page_count",
        "record_count",
        "subject_count",
        "terminal_cursor",
        "pages",
    }:
        raise PreflightError("provider/IAM pagination receipt is not exact")
    pages = pagination.get("pages")
    page_count = pagination.get("page_count")
    if (
        not isinstance(pagination.get("page_size"), int)
        or not 1 <= pagination["page_size"] <= 1000
        or not isinstance(page_count, int)
        or not 1 <= page_count <= 10000
        or not isinstance(pages, list)
        or len(pages) != page_count
        or pagination.get("terminal_cursor") != ""
        or pagination.get("subject_count") != len(users) + len(groups)
        or not isinstance(pagination.get("record_count"), int)
        or not pagination["subject_count"] <= pagination["record_count"] <= 100000
    ):
        raise PreflightError("provider/IAM pagination counts are incomplete")
    expected_request = hashlib.sha256(b"").hexdigest()
    for index, page in enumerate(pages):
        if (
            not isinstance(page, dict)
            or set(page) != {"index", "request_cursor_sha256", "response_sha256", "next_cursor_sha256"}
            or page.get("index") != index
            or page.get("request_cursor_sha256") != expected_request
            or not re.fullmatch(r"[0-9a-f]{64}", str(page.get("response_sha256", "")))
        ):
            raise PreflightError("provider/IAM pagination chain is invalid")
        next_cursor = page.get("next_cursor_sha256")
        if index + 1 == page_count:
            if next_cursor != "":
                raise PreflightError("provider/IAM pagination is not terminal")
        elif not isinstance(next_cursor, str) or not re.fullmatch(r"[0-9a-f]{64}", next_cursor):
            raise PreflightError("provider/IAM pagination cursor is invalid")
        else:
            expected_request = next_cursor
    snapshot_sha256 = hashlib.sha256(canonical(signed).encode()).hexdigest()
    return signed, users, groups, snapshot_sha256


def verified_subject_inventory(
    raw_inventory: str,
    recovery_public_key: str,
    *,
    cluster: tuple[str, str],
    rollback_valid_until: int,
    forbidden_usernames: set[str],
    provider_signed: dict[str, Any],
    provider_users: list[dict[str, Any]],
    provider_groups: list[str],
    provider_snapshot_sha256: str,
) -> tuple[list[dict[str, Any]], str]:
    signed = verified_envelope(raw_inventory, recovery_public_key, label="security subject inventory")
    expected_fields = {
        "schema",
        "inventory_id",
        "cluster",
        "provider_snapshot",
        "human_users",
        "human_groups",
        "issued_at",
        "expires_at",
        "signer_key_id",
    }
    if set(signed) != expected_fields:
        raise PreflightError("security subject inventory fields are not exact")
    expected_cluster = {
        "api_server_sha256": hashlib.sha256(cluster[0].encode()).hexdigest(),
        "kube_system_uid": cluster[1],
    }
    provider_binding = {
        "sha256": provider_snapshot_sha256,
        "snapshot_id": provider_signed["snapshot_id"],
        "provider": provider_signed["provider"],
        "adapter": provider_signed["adapter"],
        "trust_anchor_sha256": provider_signed["trust_anchor_sha256"],
        "tenant_sha256": provider_signed["tenant_sha256"],
        "query_sha256": provider_signed["query_sha256"],
        "page_count": provider_signed["pagination"]["page_count"],
        "record_count": provider_signed["pagination"]["record_count"],
        "captured_at": provider_signed["captured_at"],
        "expires_at": provider_signed["expires_at"],
        "signer_key_id": provider_signed["signer_key_id"],
    }
    now = dt.datetime.now(dt.UTC)
    issued = dt.datetime.fromisoformat(str(signed.get("issued_at", "")).replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(str(signed.get("expires_at", "")).replace("Z", "+00:00"))
    inventory_users, inventory_groups = normalized_subjects(signed, forbidden_usernames=forbidden_usernames)
    if (
        signed.get("schema") != "fs2-serve.nebius.ai/security-subject-inventory/v2"
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(signed.get("inventory_id", "")))
        or signed.get("cluster") != expected_cluster
        or signed.get("provider_snapshot") != provider_binding
        or inventory_users != provider_users
        or inventory_groups != provider_groups
        or issued.tzinfo is None
        or expires.tzinfo is None
        or issued.astimezone(dt.UTC) > now
        or expires.astimezone(dt.UTC) <= now
        or int(expires.timestamp()) < rollback_valid_until
    ):
        raise PreflightError("security subject inventory is stale or not exactly reconciled to provider/IAM")
    group_subjects = [
        {
            "username": f"fs2-security-group-probe-{hashlib.sha256(group.encode()).hexdigest()[:12]}",
            "groups": [group],
        }
        for group in inventory_groups
    ]
    return inventory_users + group_subjects, hashlib.sha256(canonical(signed).encode()).hexdigest()


def run(kubeconfig: Path, context: str, *arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed kubectl and validated arguments
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, *arguments],
        input=input_text,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise PreflightError("Kubernetes boundary preflight failed closed")
    return result.stdout


def exact_file(path: Path, *, label: str = "boundary kubeconfig") -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PreflightError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise PreflightError(f"{label} ownership or mode is unsafe")


def user_info(kubeconfig: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(run(kubeconfig, context, "auth", "whoami", "-o", "json"))["status"]["userInfo"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PreflightError("boundary whoami response is incomplete") from error
    if (
        not isinstance(value.get("username"), str)
        or not value["username"]
        or not isinstance(value.get("uid"), str)
        or not value["uid"]
        or not isinstance(value.get("groups", []), list)
        or not isinstance(value.get("extra", {}), dict)
        or not all(isinstance(group, str) and group for group in value.get("groups", []))
        or not all(
            isinstance(key, str)
            and key
            and isinstance(items, list)
            and all(isinstance(item, str) for item in items)
            for key, items in value.get("extra", {}).items()
        )
    ):
        raise PreflightError("boundary whoami tuple is invalid")
    return {
        "username": value["username"],
        "uid": value["uid"],
        "groups": sorted(value.get("groups", [])),
        "extra": {key: sorted(items) for key, items in sorted(value.get("extra", {}).items())},
    }


def credential_expiry(kubeconfig: Path, context: str) -> int:
    try:
        config = json.loads(run(kubeconfig, context, "config", "view", "--minify", "--raw", "-o", "json"))
        users = config["users"]
        if len(users) != 1:
            raise PreflightError("boundary kubeconfig must contain one selected user")
        credential = users[0]["user"]
        token = credential.get("token")
        if isinstance(token, str) and token:
            parts = token.split(".")
            if len(parts) != 3:
                raise PreflightError("boundary bearer credential is not an expiring JWT")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(payload.get("exp"), int):
                raise PreflightError("boundary JWT has no integer expiry")
            return int(payload["exp"])
        certificate = credential.get("client-certificate-data")
        if not isinstance(certificate, str) or not certificate:
            raise PreflightError("boundary credential has no cryptographically inspectable expiry")
        decoded = base64.b64decode(certificate, validate=True)
        result = subprocess.run(  # noqa: S603 -- fixed executable and input bytes
            ["openssl", "x509", "-noout", "-enddate"],
            input=decoded,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise PreflightError("boundary client certificate expiry is unreadable")
        not_after = result.stdout.decode("ascii").strip().removeprefix("notAfter=")
        return int(dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.UTC).timestamp())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PreflightError("boundary credential expiry proof is invalid") from error


def cluster_identity(kubeconfig: Path, context: str) -> tuple[str, str]:
    try:
        config = json.loads(run(kubeconfig, context, "config", "view", "--minify", "--raw", "-o", "json"))
        clusters = config["clusters"]
        server = clusters[0]["cluster"]["server"] if len(clusters) == 1 else None
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PreflightError("boundary kubeconfig cluster is incomplete") from error
    uid = run(kubeconfig, context, "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").strip()
    if not isinstance(server, str) or not server or not uid:
        raise PreflightError("boundary cluster identity is incomplete")
    return server, uid


def paginated_collection(
    kubeconfig: Path,
    context: str,
    path: str,
    *,
    page_budget: list[int],
    object_budget: list[int],
    include_rbac_rules: bool = False,
    include_csr_signer: bool = False,
) -> list[dict[str, Any]]:
    """Read one exact List snapshot with bounded Kubernetes pagination."""
    cursor = ""
    snapshot_resource_version = ""
    result: list[dict[str, Any]] = []
    while True:
        page_budget[0] -= 1
        if page_budget[0] < 0:
            raise PreflightError("Kubernetes subject discovery exceeded its page bound")
        query = "?limit=200"
        if cursor:
            query += f"&continue={quote(cursor, safe='')}"
        try:
            page = json.loads(run(kubeconfig, context, "get", "--raw", f"{path}{query}"))
        except json.JSONDecodeError as error:
            raise PreflightError("Kubernetes subject discovery returned invalid JSON") from error
        metadata = page.get("metadata", {})
        items = page.get("items")
        resource_version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or not isinstance(items, list)
            or not isinstance(resource_version, str)
            or not resource_version
        ):
            raise PreflightError("Kubernetes subject discovery returned an incomplete List")
        if snapshot_resource_version and resource_version != snapshot_resource_version:
            raise PreflightError("Kubernetes paginated List changed resourceVersion")
        snapshot_resource_version = resource_version
        for item in items:
            item_metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            name = item_metadata.get("name")
            uid = item_metadata.get("uid")
            resource_version = item_metadata.get("resourceVersion")
            if not all(isinstance(value, str) and value for value in (name, uid, resource_version)):
                raise PreflightError("Kubernetes subject discovery item identity is incomplete")
            projected: dict[str, Any] = {
                "name": name,
                "uid": uid,
                "resourceVersion": resource_version,
            }
            if include_rbac_rules:
                rules = item.get("rules", [])
                aggregation_rule = item.get("aggregationRule")
                if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
                    raise PreflightError("Kubernetes RBAC inventory contains invalid rules")
                projected["rules"] = rules
                if aggregation_rule is not None:
                    if not isinstance(aggregation_rule, dict):
                        raise PreflightError("Kubernetes RBAC aggregation is invalid")
                    projected["aggregationRule"] = aggregation_rule
            if include_csr_signer:
                spec = item.get("spec", {})
                signer_name = spec.get("signerName") if isinstance(spec, dict) else None
                if not isinstance(signer_name, str) or not signer_name:
                    raise PreflightError("Kubernetes CSR inventory contains an invalid signer")
                projected["signerName"] = signer_name
            result.append(projected)
            object_budget[0] -= 1
            if object_budget[0] < 0:
                raise PreflightError("Kubernetes subject discovery exceeded its object bound")
        next_cursor = metadata.get("continue", "")
        if not isinstance(next_cursor, str):
            raise PreflightError("Kubernetes subject discovery continuation is invalid")
        if not next_cursor:
            return result
        if next_cursor == cursor:
            raise PreflightError("Kubernetes subject discovery continuation did not advance")
        cursor = next_cursor


def kubernetes_subject_inventory(
    kubeconfig: Path,
    context: str,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[str],
    str,
]:
    """Enumerate every namespace, SA, Role, ClusterRole and CSR twice."""

    def discover() -> dict[str, Any]:
        page_budget = [2048]
        object_budget = [100000]
        namespaces = paginated_collection(
            kubeconfig,
            context,
            "/api/v1/namespaces",
            page_budget=page_budget,
            object_budget=object_budget,
        )
        if not namespaces or len({item["name"] for item in namespaces}) != len(namespaces):
            raise PreflightError("Kubernetes namespace inventory is empty or duplicated")
        service_account_inventory: dict[str, list[dict[str, str]]] = {}
        role_inventory: dict[str, list[dict[str, str]]] = {}
        for namespace in sorted(namespaces, key=lambda value: value["name"]):
            name = namespace["name"]
            service_accounts = paginated_collection(
                kubeconfig,
                context,
                f"/api/v1/namespaces/{quote(name, safe='')}/serviceaccounts",
                page_budget=page_budget,
                object_budget=object_budget,
            )
            if len({item["name"] for item in service_accounts}) != len(service_accounts):
                raise PreflightError("Kubernetes ServiceAccount inventory contains duplicates")
            roles = paginated_collection(
                kubeconfig,
                context,
                f"/apis/rbac.authorization.k8s.io/v1/namespaces/{quote(name, safe='')}/roles",
                page_budget=page_budget,
                object_budget=object_budget,
                include_rbac_rules=True,
            )
            if len({item["name"] for item in roles}) != len(roles):
                raise PreflightError("Kubernetes Role inventory contains duplicates")
            service_account_inventory[name] = sorted(service_accounts, key=lambda value: value["name"])
            role_inventory[name] = sorted(roles, key=lambda value: value["name"])
        cluster_roles = paginated_collection(
            kubeconfig,
            context,
            "/apis/rbac.authorization.k8s.io/v1/clusterroles",
            page_budget=page_budget,
            object_budget=object_budget,
            include_rbac_rules=True,
        )
        if len({item["name"] for item in cluster_roles}) != len(cluster_roles):
            raise PreflightError("Kubernetes ClusterRole inventory contains duplicates")
        certificate_signing_requests = paginated_collection(
            kubeconfig,
            context,
            "/apis/certificates.k8s.io/v1/certificatesigningrequests",
            page_budget=page_budget,
            object_budget=object_budget,
            include_csr_signer=True,
        )
        return {
            "namespaces": sorted(namespaces, key=lambda value: value["name"]),
            "service_accounts": service_account_inventory,
            "roles": role_inventory,
            "cluster_roles": sorted(cluster_roles, key=lambda value: value["name"]),
            "certificate_signing_requests": sorted(
                certificate_signing_requests,
                key=lambda value: value["name"],
            ),
        }

    first = discover()
    second = discover()
    if first != second:
        raise PreflightError("Kubernetes subject inventory drifted during authorization proof")
    digest = hashlib.sha256(canonical(first).encode()).hexdigest()
    csr_signers = sorted(
        {item["signerName"] for item in first["certificate_signing_requests"]}
    )
    return first["service_accounts"], first["roles"], first["cluster_roles"], csr_signers, digest


def rbac_rule_authorization_targets(
    roles: dict[str, list[dict[str, Any]]],
    cluster_roles: list[dict[str, Any]],
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]], set[str]]:
    """Derive every named impersonation, delegation and signer grant."""
    impersonation: set[tuple[str, str, str]] = set()
    delegation: set[tuple[str, str, str]] = set()
    signers: set[str] = set()
    scoped_roles = [
        (namespace, role)
        for namespace, namespace_roles in roles.items()
        for role in namespace_roles
    ] + [("", role) for role in cluster_roles]
    for role_namespace, role in scoped_roles:
        for rule in role.get("rules", []):
            api_groups = rule.get("apiGroups", [])
            resources = rule.get("resources", [])
            verbs = rule.get("verbs", [])
            resource_names = rule.get("resourceNames", [])
            if not all(
                isinstance(values, list) and all(isinstance(value, str) for value in values)
                for values in (api_groups, resources, verbs, resource_names)
            ):
                raise PreflightError("Kubernetes RBAC rule fields are not exact")
            verb_set = set(verbs)
            resource_set = set(resources)
            group_set = set(api_groups)
            if "impersonate" in verb_set or "*" in verb_set:
                for name in resource_names:
                    for resource, group in (
                        ("users", ""),
                        ("groups", ""),
                        ("serviceaccounts", ""),
                        ("uids", "authentication.k8s.io"),
                        ("userextras", "authentication.k8s.io"),
                    ):
                        if (group in group_set or "*" in group_set) and (
                            resource in resource_set or "*" in resource_set
                        ):
                            qualified = (
                                f"{resource}.authentication.k8s.io"
                                if group == "authentication.k8s.io"
                                else resource
                            )
                            target_namespace = role_namespace if resource == "serviceaccounts" else ""
                            impersonation.add((qualified, name, target_namespace))
            if {"bind", "escalate", "*"} & verb_set and (
                "rbac.authorization.k8s.io" in group_set or "*" in group_set
            ):
                for name in resource_names:
                    if "clusterroles" in resource_set or "*" in resource_set:
                        delegation.add(("clusterroles.rbac.authorization.k8s.io", name, ""))
                    if "roles" in resource_set or "*" in resource_set:
                        delegation.add(("roles.rbac.authorization.k8s.io", name, role_namespace))
            if {"approve", "sign", "*"} & verb_set and (
                "certificates.k8s.io" in group_set or "*" in group_set
            ) and ("signers" in resource_set or "*" in resource_set):
                signers.update(resource_names)
    return impersonation, delegation, signers


def rotation_binding_contract(
    kubeconfig: Path,
    context: str,
    *,
    resource: str,
    name: str,
    namespace: str,
    role_kind: str,
    role_name: str,
    before_subjects: list[str],
    target_subjects: list[str],
) -> tuple[dict[str, Any], str]:
    try:
        arguments = ["get", resource, name, "-o", "json"]
        if namespace:
            arguments.extend(["--namespace", namespace])
        binding = json.loads(run(kubeconfig, context, *arguments))
    except json.JSONDecodeError as error:
        raise PreflightError("epoch rotation binding is not valid JSON") from error
    metadata = binding.get("metadata", {}) if isinstance(binding, dict) else {}
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    identity = {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace", ""),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
    }
    expected_role_ref = {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": role_kind,
        "name": role_name,
    }
    def subject_set(values: list[str]) -> list[dict[str, str]]:
        return sorted(
            [
                {"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": value}
                for value in values
            ],
            key=canonical,
        )
    live_subjects = binding.get("subjects") if isinstance(binding, dict) else None
    before = subject_set(before_subjects)
    target = subject_set(target_subjects)
    normalized_live = sorted(live_subjects, key=canonical) if isinstance(live_subjects, list) else []
    state = "before" if normalized_live == before else "target" if normalized_live == target else "invalid"
    if (
        identity["name"] != name
        or identity["namespace"] != namespace
        or not all(
            isinstance(item, str) and item
            for item in (identity["name"], identity["uid"], identity["resourceVersion"])
        )
        or labels.get("fs2.nebius.ai/network-policy-boundary") != "permanent"
        or binding.get("roleRef") != expected_role_ref
        or state == "invalid"
    ):
        raise PreflightError("epoch rotation binding is outside its exact before/target states")
    return {
        "metadata": identity,
        "roleRef": binding["roleRef"],
        "subjects": normalized_live,
        "state": state,
    }, state


def external_role_contract(
    kubeconfig: Path,
    context: str,
    *,
    resource: str,
    name: str,
    namespace: str,
    expected_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    arguments = ["get", resource, name, "-o", "json"]
    if namespace:
        arguments.extend(["--namespace", namespace])
    try:
        role = json.loads(run(kubeconfig, context, *arguments))
    except json.JSONDecodeError as error:
        raise PreflightError("external role handoff is not valid JSON") from error
    metadata = role.get("metadata", {}) if isinstance(role, dict) else {}
    labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
    identity = {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace", ""),
        "uid": metadata.get("uid"),
        "resourceVersion": metadata.get("resourceVersion"),
    }
    rules = role.get("rules") if isinstance(role, dict) else None
    if (
        identity["name"] != name
        or identity["namespace"] != namespace
        or not all(isinstance(item, str) and item for item in identity.values() if item != "")
        or labels.get("fs2.nebius.ai/network-policy-boundary") != "permanent"
        or not isinstance(rules, list)
        or sorted(rules, key=canonical) != sorted(expected_rules, key=canonical)
    ):
        raise PreflightError("external immutable role definition is not least-privilege exact")
    return {"resource": resource, "metadata": identity, "rules": rules}


def auditor_bootstrap_contract(
    kubeconfig: Path,
    context: str,
    bootstrap_username: str,
    prior_bootstrap_username: str,
    successor_bootstrap_username: str,
    gateway_namespace: str,
    controller_namespace: str,
) -> tuple[str, str, dict[str, Any], str]:
    """Verify the externally provisioned, non-destructively imported auditor."""
    expected_rules = [
        {"apiGroups": [""], "resources": ["namespaces", "serviceaccounts"], "verbs": ["get", "list"]},
        {
            "apiGroups": ["authorization.k8s.io"],
            "resources": ["subjectaccessreviews"],
            "verbs": ["create"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["roles", "clusterroles"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["certificates.k8s.io"],
            "resources": ["certificatesigningrequests"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["clusterrolebindings"],
            "resourceNames": [
                "fs2-network-policy-security-owner",
                "fs2-network-policy-security-auditor",
                "fs2-network-policy-security-bootstrap",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": [
                "fs2-network-policy-transition",
                "fs2-network-policy-boundary-topology",
                "fs2-network-policy-boundary-parameters",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "resourceNames": ["fs2-network-policy-transition"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "resourceNames": [
                "fs2-serve-control-plane-public-envoy-transition-guard",
                "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
                "fs2-serve-control-plane-envoy-default-deny",
            ],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["admissionregistration.k8s.io"],
            "resources": ["validatingadmissionpolicies", "validatingadmissionpolicybindings"],
            "resourceNames": ["fs2-network-policy-boundary"],
            "verbs": ["get"],
        },
    ]

    roles = [
        external_role_contract(
            kubeconfig,
            context,
            resource="clusterrole",
            name="fs2-network-policy-security-auditor",
            namespace="",
            expected_rules=expected_rules,
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="clusterrole",
            name="fs2-network-policy-security-owner",
            namespace="",
            expected_rules=[
                {
                    "apiGroups": ["admissionregistration.k8s.io"],
                    "resources": ["validatingadmissionpolicies", "validatingadmissionpolicybindings"],
                    "resourceNames": ["fs2-network-policy-boundary"],
                    "verbs": ["get", "patch", "update"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["namespaces"],
                    "resourceNames": list(
                        dict.fromkeys(
                            ["kube-system", "fs2-system", gateway_namespace, controller_namespace]
                        )
                    ),
                    "verbs": ["get"],
                },
                {
                    "apiGroups": ["rbac.authorization.k8s.io"],
                    "resources": ["clusterroles", "clusterrolebindings"],
                    "resourceNames": [
                        "fs2-network-policy-security-owner",
                        "fs2-network-policy-security-auditor",
                    ],
                    "verbs": ["get"],
                },
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="clusterrole",
            name="fs2-network-policy-security-bootstrap",
            namespace="",
            expected_rules=[
                {
                    "apiGroups": ["rbac.authorization.k8s.io"],
                    "resources": ["clusterrolebindings"],
                    "resourceNames": [
                        "fs2-network-policy-security-owner",
                        "fs2-network-policy-security-auditor",
                        "fs2-network-policy-security-bootstrap",
                    ],
                    "verbs": ["get", "patch", "update"],
                }
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition",
            namespace="fs2-system",
            expected_rules=[
                {
                    "apiGroups": ["coordination.k8s.io"],
                    "resources": ["leases"],
                    "resourceNames": ["fs2-network-policy-transition"],
                    "verbs": ["get", "patch", "update"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": [
                        "fs2-network-policy-transition",
                        "fs2-network-policy-boundary-topology",
                        "fs2-network-policy-boundary-parameters",
                    ],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": [
                        "fs2-network-policy-transition",
                        "fs2-network-policy-boundary-parameters",
                    ],
                    "verbs": ["get", "patch", "update"],
                },
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition-bootstrap",
            namespace="fs2-system",
            expected_rules=[
                {
                    "apiGroups": ["rbac.authorization.k8s.io"],
                    "resources": ["rolebindings"],
                    "resourceNames": [
                        "fs2-network-policy-transition",
                        "fs2-network-policy-transition-bootstrap",
                    ],
                    "verbs": ["get", "patch", "update"],
                }
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition-gateway",
            namespace=gateway_namespace,
            expected_rules=[
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "resourceNames": [
                        "fs2-serve-control-plane-public-envoy-transition-guard",
                        "fs2-serve-control-plane-envoy-default-deny",
                    ],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "resourceNames": [
                        "fs2-serve-control-plane-public-envoy-transition-guard",
                        "fs2-serve-control-plane-envoy-default-deny",
                    ],
                    "verbs": ["patch", "update"],
                },
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition-gateway-bootstrap",
            namespace=gateway_namespace,
            expected_rules=[
                {
                    "apiGroups": ["rbac.authorization.k8s.io"],
                    "resources": ["rolebindings"],
                    "resourceNames": [
                        "fs2-network-policy-transition-gateway",
                        "fs2-network-policy-transition-gateway-bootstrap",
                    ],
                    "verbs": ["get", "patch", "update"],
                }
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition-controller",
            namespace=controller_namespace,
            expected_rules=[
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "resourceNames": ["fs2-serve-control-plane-envoy-controller-xds-transition-guard"],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "resourceNames": ["fs2-serve-control-plane-envoy-controller-xds-transition-guard"],
                    "verbs": ["patch", "update"],
                },
            ],
        ),
        external_role_contract(
            kubeconfig,
            context,
            resource="role",
            name="fs2-network-policy-transition-controller-bootstrap",
            namespace=controller_namespace,
            expected_rules=[
                {
                    "apiGroups": ["rbac.authorization.k8s.io"],
                    "resources": ["rolebindings"],
                    "resourceNames": [
                        "fs2-network-policy-transition-controller",
                        "fs2-network-policy-transition-controller-bootstrap",
                    ],
                    "verbs": ["get", "patch", "update"],
                }
            ],
        ),
    ]
    binding_evidence, state = rotation_binding_contract(
        kubeconfig,
        context,
        resource="clusterrolebinding",
        name="fs2-network-policy-security-auditor",
        namespace="",
        role_kind="ClusterRole",
        role_name="fs2-network-policy-security-auditor",
        before_subjects=[prior_bootstrap_username, bootstrap_username],
        target_subjects=[bootstrap_username, successor_bootstrap_username],
    )
    evidence = {
        "roles": roles,
        "binding": binding_evidence,
    }
    return hashlib.sha256(
        canonical(
            evidence
        ).encode()
    ).hexdigest(), state, evidence, hashlib.sha256(canonical(roles).encode()).hexdigest()


def can_i(kubeconfig: Path, context: str, expected: str, *arguments: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed kubectl and validated exact arguments
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, "auth", "can-i", *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.stdout.strip() != expected or (expected == "yes" and result.returncode != 0):
        raise PreflightError("boundary authorization is broader or narrower than its contract")


def subject_denied(
    kubeconfig: Path,
    context: str,
    subject: dict[str, Any],
    *,
    verb: str,
    group: str,
    resource: str,
    namespace: str = "",
    name: str = "",
    subresource: str = "",
) -> None:
    attributes = {"verb": verb, "group": group, "resource": resource}
    for key, value in (("namespace", namespace), ("name", name), ("subresource", subresource)):
        if value:
            attributes[key] = value
    review: dict[str, Any] = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": subject["username"],
            "groups": sorted(subject["groups"]),
            "resourceAttributes": attributes,
        },
    }
    if subject.get("uid"):
        review["spec"]["uid"] = subject["uid"]
    if subject.get("extra"):
        review["spec"]["extra"] = {
            key: sorted(values) for key, values in sorted(subject["extra"].items())
        }
    try:
        response = json.loads(
            run(
                kubeconfig,
                context,
                "create",
                "--raw",
                "/apis/authorization.k8s.io/v1/subjectaccessreviews",
                "-f",
                "-",
                input_text=canonical(review),
            )
        )
    except json.JSONDecodeError as error:
        raise PreflightError("human-subject authorization review is invalid") from error
    if response.get("status", {}).get("allowed") is not False:
        raise PreflightError("a reviewed human subject can cross the security boundary")


def parse_query() -> dict[str, Any]:
    try:
        query = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise PreflightError("external preflight query is invalid") from error
    required = {
        "mode",
        "context",
        "kube_system_uid",
        "release_kubeconfig",
        "security_kubeconfig",
        "bootstrap_kubeconfig",
        "prior_security_kubeconfig",
        "prior_bootstrap_kubeconfig",
        "release_identity",
        "security_identity",
        "bootstrap_identity",
        "prior_security_identity",
        "prior_bootstrap_identity",
        "subject_inventory",
        "provider_snapshot_path",
        "provider_trust_anchor_path",
        "provider_adapter_path",
        "recovery_public_key",
        "minimum_rollback_seconds",
        "gateway_namespace",
        "controller_namespace",
        "security_owner_username",
        "security_bootstrap_username",
        "prior_security_owner_username",
        "prior_security_bootstrap_username",
        "successor_security_owner_username",
        "successor_security_bootstrap_username",
        "peer_uid",
        "peer_gid",
        "socket_path",
        "identity_epoch",
        "prior_identity_epoch",
        "successor_identity_epoch",
    }
    if (
        not isinstance(query, dict)
        or set(query) != required
        or not all(isinstance(value, str) for value in query.values())
    ):
        raise PreflightError("external preflight query fields are not exact strings")
    return query


def main() -> int:
    try:
        query = parse_query()
        epochs = [query["prior_identity_epoch"], query["identity_epoch"], query["successor_identity_epoch"]]
        if len(set(epochs)) != 3 or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", epoch) for epoch in epochs
        ):
            raise PreflightError("prior, current and successor identity epochs are not exact and disjoint")

        def epoch_principal(role: str, epoch: str) -> str:
            return f"fs2-np-{role}-{hashlib.sha256(epoch.encode()).hexdigest()[:16]}"

        expected_principals = {
            "release": epoch_principal("release", query["identity_epoch"]),
            "security": epoch_principal("security-owner", query["identity_epoch"]),
            "bootstrap": epoch_principal("security-bootstrap", query["identity_epoch"]),
            "prior_security": epoch_principal("security-owner", query["prior_identity_epoch"]),
            "prior_bootstrap": epoch_principal("security-bootstrap", query["prior_identity_epoch"]),
            "successor_security": epoch_principal("security-owner", query["successor_identity_epoch"]),
            "successor_bootstrap": epoch_principal("security-bootstrap", query["successor_identity_epoch"]),
        }
        if (
            query["security_owner_username"] != expected_principals["security"]
            or query["security_bootstrap_username"] != expected_principals["bootstrap"]
            or query["prior_security_owner_username"] != expected_principals["prior_security"]
            or query["prior_security_bootstrap_username"] != expected_principals["prior_bootstrap"]
            or query["successor_security_owner_username"] != expected_principals["successor_security"]
            or query["successor_security_bootstrap_username"] != expected_principals["successor_bootstrap"]
        ):
            raise PreflightError("security principals are not derived from their exact epochs")
        paths = {
            role: Path(query[field])
            for role, field in (
                ("release", "release_kubeconfig"),
                ("security", "security_kubeconfig"),
                ("bootstrap", "bootstrap_kubeconfig"),
                ("prior_security", "prior_security_kubeconfig"),
                ("prior_bootstrap", "prior_bootstrap_kubeconfig"),
            )
        }
        if len({str(path) for path in paths.values()}) != 5 or not all(path.is_absolute() for path in paths.values()):
            raise PreflightError("boundary kubeconfig paths are not distinct absolute paths")
        for path in paths.values():
            exact_file(path)
        provider_snapshot_path = Path(query["provider_snapshot_path"])
        provider_trust_anchor_path = Path(query["provider_trust_anchor_path"])
        provider_adapter_path = Path(query["provider_adapter_path"])
        live_identity_uids: list[str] = []
        live_identity_groups: list[str] = []
        live_identity_extra_keys: list[str] = []
        if query["mode"] == "public":
            exact_file(provider_snapshot_path, label="provider/IAM subject snapshot")
            if any(
                paths[role].parent.name != query["identity_epoch"]
                for role in ("release", "security", "bootstrap")
            ):
                raise PreflightError("current credential files are not in the current immutable epoch")
            if any(
                paths[role].parent.name != query["prior_identity_epoch"]
                for role in ("prior_security", "prior_bootstrap")
            ):
                raise PreflightError("prior credential files are not in the prior immutable epoch")
            if provider_snapshot_path.parent.name != query["identity_epoch"]:
                raise PreflightError("provider/IAM snapshot is not in the current immutable epoch")
        credential_hashes = {
            role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in sorted(paths.items())
        }
        credential_set_sha256 = hashlib.sha256(canonical(credential_hashes).encode()).hexdigest()
        if query["mode"] == "public":
            socket_path = Path(query["socket_path"])
            socket_parent = socket_path.parent
            try:
                socket_parent_metadata = socket_parent.lstat()
            except OSError as error:
                raise PreflightError("versioned security socket parent is unavailable") from error
            try:
                socket_path.lstat()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise PreflightError("versioned security socket path cannot be inspected") from error
            else:
                raise PreflightError("versioned security socket path must be absent")
            if (
                not socket_path.is_absolute()
                or socket_parent.name != query["identity_epoch"]
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", query["identity_epoch"])
                or not stat.S_ISDIR(socket_parent_metadata.st_mode)
                or stat.S_ISLNK(socket_parent_metadata.st_mode)
                or socket_parent_metadata.st_uid != os.geteuid()
                or socket_parent_metadata.st_gid != int(query["peer_gid"])
                or stat.S_IMODE(socket_parent_metadata.st_mode) != 0o2710
            ):
                raise PreflightError("versioned security socket parent is not the exact setgid contract")
        clusters = {role: cluster_identity(path, query["context"]) for role, path in paths.items()}
        if len(set(clusters.values())) != 1 or next(iter(clusters.values()))[1] != query["kube_system_uid"]:
            raise PreflightError("boundary identities are not bound to the same reviewed cluster")

        if query["mode"] == "public":
            expected = {
                role: json.loads(query[field])
                for role, field in (
                    ("release", "release_identity"),
                    ("security", "security_identity"),
                    ("bootstrap", "bootstrap_identity"),
                )
            }
            prior_expected = {
                role: json.loads(query[field])
                for role, field in (
                    ("prior_security", "prior_security_identity"),
                    ("prior_bootstrap", "prior_bootstrap_identity"),
                )
            }
            actual = {
                role: user_info(paths[role], query["context"])
                for role in ("release", "security", "bootstrap")
            }
            normalized_expected = {
                role: {
                    "username": value["username"],
                    "uid": value["uid"],
                    "groups": sorted(value["groups"]),
                    "extra": {key: sorted(items) for key, items in sorted(value["extra"].items())},
                }
                for role, value in expected.items()
            }
            if actual != normalized_expected:
                raise PreflightError("live whoami tuples do not match the reviewed identities")
            if any(
                actual[role]["username"] != expected_principals[role]
                for role in ("release", "security", "bootstrap")
            ):
                raise PreflightError("live whoami usernames are not bound to the current identity epoch")
            for role, value in prior_expected.items():
                if (
                    not isinstance(value, dict)
                    or value.get("username") != expected_principals[role]
                    or not isinstance(value.get("uid"), str)
                    or not value["uid"]
                    or not isinstance(value.get("groups"), list)
                    or not value["groups"]
                    or not isinstance(value.get("extra"), dict)
                ):
                    raise PreflightError("prior epoch identity tuple is incomplete")
            prior_actual = {role: user_info(paths[role], query["context"]) for role in prior_expected}
            normalized_prior_expected = {
                role: {
                    "username": value["username"],
                    "uid": value["uid"],
                    "groups": sorted(value["groups"]),
                    "extra": {key: sorted(items) for key, items in sorted(value["extra"].items())},
                }
                for role, value in prior_expected.items()
            }
            if prior_actual != normalized_prior_expected:
                raise PreflightError("prior live whoami tuples do not match the reviewed retired identities")
            all_actual = {**actual, **prior_actual}
            live_identity_uids = sorted({value["uid"] for value in all_actual.values()})
            live_identity_groups = sorted(
                {group for value in all_actual.values() for group in value["groups"]}
            )
            live_identity_extra_keys = sorted(
                {key for value in all_actual.values() for key in value["extra"]}
            )
            if len({value["username"] for value in all_actual.values()}) != 5 or len(
                {value["uid"] for value in all_actual.values()}
            ) != 5:
                raise PreflightError("current and prior identity tuples are not disjoint")
            if len({value["username"] for value in actual.values()}) != 3 or len(
                {value["uid"] for value in actual.values()}
            ) != 3:
                raise PreflightError("release, security and bootstrap identities are not disjoint")
            allowed_shared = {"system:authenticated", "system:serviceaccounts"}
            roles = list(all_actual)
            for index, left in enumerate(roles):
                for right in roles[index + 1 :]:
                    if (set(all_actual[left]["groups"]) & set(all_actual[right]["groups"])) - allowed_shared:
                        raise PreflightError("boundary identities share an unreviewed group")
            now = int(dt.datetime.now(dt.UTC).timestamp())
            limits = {"release": 28800, "security": 28800, "bootstrap": 900}
            expiries: dict[str, int] = {}
            for role, value in expected.items():
                configured = int(dt.datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")).timestamp())
                expiries[role] = configured
                if credential_expiry(paths[role], query["context"]) != configured:
                    raise PreflightError("configured identity expiry is not cryptographically bound")
                if not now < configured <= now + limits[role]:
                    raise PreflightError("boundary credential lifetime exceeds its limit")
            for role, value in prior_expected.items():
                configured = int(dt.datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")).timestamp())
                if credential_expiry(paths[role], query["context"]) != configured:
                    raise PreflightError("prior identity expiry is not cryptographically bound")
                if not now < configured <= now + 28800:
                    raise PreflightError("prior credential must remain valid during retirement proof")
            minimum_rollback_seconds = int(query["minimum_rollback_seconds"])
            rollback_valid_until = min(expiries["release"], expiries["security"])
            if (
                minimum_rollback_seconds < 3600
                or rollback_valid_until < expiries["bootstrap"] + minimum_rollback_seconds
            ):
                raise PreflightError("boundary credentials do not preserve the minimum post-bootstrap rollback window")
            provider_trust, provider_trust_anchor_sha256, provider_adapter_sha256 = (
                verified_provider_trust_anchor(
                    provider_trust_anchor_path,
                    provider_adapter_path,
                    rollback_valid_until=rollback_valid_until,
                )
            )
            if provider_trust["snapshot_public_key"] == query["recovery_public_key"]:
                raise PreflightError("provider/IAM and recovery inventory authorities must be disjoint")
            provider_snapshot_bytes, provider_snapshot_metadata = descriptor_bytes(
                provider_snapshot_path,
                maximum=16777216,
                label="provider/IAM subject snapshot",
            )
            if (
                not stat.S_ISREG(provider_snapshot_metadata.st_mode)
                or stat.S_IMODE(provider_snapshot_metadata.st_mode) != 0o600
                or provider_snapshot_metadata.st_uid != os.geteuid()
            ):
                raise PreflightError("provider/IAM subject snapshot custody changed")
            try:
                provider_snapshot_text = provider_snapshot_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise PreflightError("provider/IAM subject snapshot is not UTF-8") from error
            provider_signed, provider_users, provider_groups, provider_snapshot_sha256 = (
                verified_provider_snapshot(
                    provider_snapshot_text,
                    provider_trust,
                    trust_anchor_sha256=provider_trust_anchor_sha256,
                    adapter_sha256=provider_adapter_sha256,
                    rollback_valid_until=rollback_valid_until,
                    forbidden_usernames=set(expected_principals.values()),
                )
            )
            humans, inventory_sha256 = verified_subject_inventory(
                query["subject_inventory"],
                query["recovery_public_key"],
                cluster=next(iter(clusters.values())),
                rollback_valid_until=rollback_valid_until,
                forbidden_usernames=set(expected_principals.values()),
                provider_signed=provider_signed,
                provider_users=provider_users,
                provider_groups=provider_groups,
                provider_snapshot_sha256=provider_snapshot_sha256,
            )
        else:
            humans = []
            prior_expected = {}
            provider_users = []
            provider_groups = []
            inventory_sha256 = hashlib.sha256(b"internal-only").hexdigest()
            provider_snapshot_sha256 = hashlib.sha256(b"internal-only-provider").hexdigest()
            provider_trust_anchor_sha256 = hashlib.sha256(b"internal-only-trust-anchor").hexdigest()
            provider_adapter_sha256 = hashlib.sha256(b"internal-only-provider-adapter").hexdigest()
            auditor_bootstrap_sha256 = hashlib.sha256(b"internal-only-auditor-bootstrap").hexdigest()
            external_role_bundle_sha256 = hashlib.sha256(b"internal-only-role-bundle").hexdigest()

        release = paths["release"]
        security = paths["security"]
        bootstrap = paths["bootstrap"]
        prior_security = paths["prior_security"]
        prior_bootstrap = paths["prior_bootstrap"]
        context = query["context"]
        if query["mode"] == "public":
            (
                auditor_bootstrap_sha256,
                auditor_state,
                auditor_evidence,
                external_role_bundle_sha256,
            ) = auditor_bootstrap_contract(
                bootstrap,
                context,
                expected_principals["bootstrap"],
                expected_principals["prior_bootstrap"],
                expected_principals["successor_bootstrap"],
                query["gateway_namespace"],
                query["controller_namespace"],
            )
            before_mutation_subjects = [
                expected_principals["prior_security"],
                expected_principals["security"],
                expected_principals["bootstrap"],
            ]
            target_mutation_subjects = [
                expected_principals["security"],
                expected_principals["successor_security"],
                expected_principals["successor_bootstrap"],
            ]
            before_bootstrap_subjects = [
                expected_principals["prior_bootstrap"],
                expected_principals["bootstrap"],
            ]
            target_bootstrap_subjects = [
                expected_principals["bootstrap"],
                expected_principals["successor_bootstrap"],
            ]
            binding_specs = (
                (
                    "cluster",
                    "clusterrolebinding",
                    "fs2-network-policy-security-owner",
                    "",
                    "ClusterRole",
                    "fs2-network-policy-security-owner",
                    before_mutation_subjects,
                    target_mutation_subjects,
                ),
                (
                    "cluster_bootstrap",
                    "clusterrolebinding",
                    "fs2-network-policy-security-bootstrap",
                    "",
                    "ClusterRole",
                    "fs2-network-policy-security-bootstrap",
                    before_bootstrap_subjects,
                    target_bootstrap_subjects,
                ),
                (
                    "state",
                    "rolebinding",
                    "fs2-network-policy-transition",
                    "fs2-system",
                    "Role",
                    "fs2-network-policy-transition",
                    before_mutation_subjects,
                    target_mutation_subjects,
                ),
                (
                    "state_bootstrap",
                    "rolebinding",
                    "fs2-network-policy-transition-bootstrap",
                    "fs2-system",
                    "Role",
                    "fs2-network-policy-transition-bootstrap",
                    before_bootstrap_subjects,
                    target_bootstrap_subjects,
                ),
                (
                    "gateway",
                    "rolebinding",
                    "fs2-network-policy-transition-gateway",
                    query["gateway_namespace"],
                    "Role",
                    "fs2-network-policy-transition-gateway",
                    before_mutation_subjects,
                    target_mutation_subjects,
                ),
                (
                    "gateway_bootstrap",
                    "rolebinding",
                    "fs2-network-policy-transition-gateway-bootstrap",
                    query["gateway_namespace"],
                    "Role",
                    "fs2-network-policy-transition-gateway-bootstrap",
                    before_bootstrap_subjects,
                    target_bootstrap_subjects,
                ),
                (
                    "controller",
                    "rolebinding",
                    "fs2-network-policy-transition-controller",
                    query["controller_namespace"],
                    "Role",
                    "fs2-network-policy-transition-controller",
                    before_mutation_subjects,
                    target_mutation_subjects,
                ),
                (
                    "controller_bootstrap",
                    "rolebinding",
                    "fs2-network-policy-transition-controller-bootstrap",
                    query["controller_namespace"],
                    "Role",
                    "fs2-network-policy-transition-controller-bootstrap",
                    before_bootstrap_subjects,
                    target_bootstrap_subjects,
                ),
            )
            rotation_binding_states = {"auditor": auditor_state}
            rotation_binding_evidence = {"auditor": auditor_evidence}
            for (
                key,
                resource,
                name,
                namespace,
                role_kind,
                role_name,
                before_subjects,
                target_subjects,
            ) in binding_specs:
                evidence, state = rotation_binding_contract(
                    bootstrap,
                    context,
                    resource=resource,
                    name=name,
                    namespace=namespace,
                    role_kind=role_kind,
                    role_name=role_name,
                    before_subjects=before_subjects,
                    target_subjects=target_subjects,
                )
                rotation_binding_states[key] = state
                rotation_binding_evidence[key] = evidence
            state_values = set(rotation_binding_states.values())
            rotation_phase = (
                "preapply"
                if state_values == {"before"}
                else "postapply"
                if state_values == {"target"}
                else "resume"
            )
            rotation_binding_state_sha256 = hashlib.sha256(
                canonical(rotation_binding_evidence).encode()
            ).hexdigest()
        else:
            rotation_binding_states = {
                "auditor": "target",
                "cluster": "target",
                "state": "target",
                "gateway": "target",
                "controller": "target",
                "cluster_bootstrap": "target",
                "state_bootstrap": "target",
                "gateway_bootstrap": "target",
                "controller_bootstrap": "target",
            }
            rotation_phase = "postapply"
            rotation_binding_state_sha256 = hashlib.sha256(b"internal-only-rotation-state").hexdigest()
        protected_cluster = (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
        for resource in protected_cluster:
            for verb in ("patch", "update"):
                can_i(release, context, "no", verb, resource, "--resource-name=fs2-network-policy-boundary")
                can_i(security, context, "no", verb, resource)
                can_i(security, context, "yes", verb, resource, "--resource-name=fs2-network-policy-boundary")
                can_i(bootstrap, context, "no", verb, resource)
                can_i(
                    bootstrap,
                    context,
                    "yes" if rotation_binding_states["cluster"] == "before" else "no",
                    verb,
                    resource,
                    "--resource-name=fs2-network-policy-boundary",
                )
                can_i(
                    prior_security,
                    context,
                    "yes" if rotation_binding_states["cluster"] == "before" else "no",
                    verb,
                    resource,
                    "--resource-name=fs2-network-policy-boundary",
                )
                can_i(prior_bootstrap, context, "no", verb, resource, "--resource-name=fs2-network-policy-boundary")
            can_i(release, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(security, context, "no", "delete", resource)
            can_i(security, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(bootstrap, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(prior_security, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(prior_bootstrap, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(release, context, "no", "deletecollection", resource)
            can_i(security, context, "no", "deletecollection", resource)
            can_i(bootstrap, context, "no", "deletecollection", resource)
            can_i(prior_security, context, "no", "deletecollection", resource)
            can_i(prior_bootstrap, context, "no", "deletecollection", resource)
            can_i(security, context, "no", "create", resource)
            can_i(bootstrap, context, "no", "create", resource)
            can_i(prior_security, context, "no", "create", resource)
            can_i(prior_bootstrap, context, "no", "create", resource)

        namespaced = (
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-public-envoy-transition-guard",
                query["gateway_namespace"],
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-default-deny",
                query["gateway_namespace"],
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
                query["controller_namespace"],
            ),
            ("configmaps", "fs2-network-policy-transition", "fs2-system"),
            ("configmaps", "fs2-network-policy-boundary-topology", "fs2-system"),
            ("configmaps", "fs2-network-policy-boundary-parameters", "fs2-system"),
            ("leases.coordination.k8s.io", "fs2-network-policy-transition", "fs2-system"),
        )
        rbac_objects = (
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-owner", ""),
            ("clusterrolebindings.rbac.authorization.k8s.io", "fs2-network-policy-security-owner", ""),
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-auditor", ""),
            ("clusterrolebindings.rbac.authorization.k8s.io", "fs2-network-policy-security-auditor", ""),
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-bootstrap", ""),
            ("clusterrolebindings.rbac.authorization.k8s.io", "fs2-network-policy-security-bootstrap", ""),
            ("roles.rbac.authorization.k8s.io", "fs2-network-policy-transition", "fs2-system"),
            ("rolebindings.rbac.authorization.k8s.io", "fs2-network-policy-transition", "fs2-system"),
            ("roles.rbac.authorization.k8s.io", "fs2-network-policy-transition-bootstrap", "fs2-system"),
            ("rolebindings.rbac.authorization.k8s.io", "fs2-network-policy-transition-bootstrap", "fs2-system"),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway",
                query["gateway_namespace"],
            ),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway-bootstrap",
                query["gateway_namespace"],
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway-bootstrap",
                query["gateway_namespace"],
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway",
                query["gateway_namespace"],
            ),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-controller",
                query["controller_namespace"],
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-controller",
                query["controller_namespace"],
            ),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-controller-bootstrap",
                query["controller_namespace"],
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-controller-bootstrap",
                query["controller_namespace"],
            ),
        )
        for resource, name, namespace in namespaced:
            binding_key = (
                "gateway"
                if resource.startswith("networkpolicies.")
                and name != "fs2-serve-control-plane-envoy-controller-xds-transition-guard"
                else "controller"
                if resource.startswith("networkpolicies.")
                else "state"
            )
            for verb in ("patch", "update"):
                can_i(release, context, "no", verb, resource, f"--resource-name={name}", "--namespace", namespace)
                can_i(security, context, "no", verb, resource, "--namespace", namespace)
                can_i(
                    security,
                    context,
                    "no" if name == "fs2-network-policy-boundary-topology" else "yes",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                can_i(bootstrap, context, "no", verb, resource, "--namespace", namespace)
                can_i(
                    bootstrap,
                    context,
                    "yes" if rotation_binding_states[binding_key] == "before" else "no",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                can_i(
                    prior_security,
                    context,
                    (
                        "yes"
                        if rotation_binding_states[binding_key] == "before"
                        and name != "fs2-network-policy-boundary-topology"
                        else "no"
                    ),
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                can_i(
                    prior_bootstrap,
                    context,
                    "no",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
            for verb in ("delete", "deletecollection"):
                arguments = (verb, resource, "--namespace", namespace)
                can_i(security, context, "no", *arguments)
                can_i(bootstrap, context, "no", *arguments)
                can_i(prior_security, context, "no", *arguments)
                can_i(prior_bootstrap, context, "no", *arguments)
            can_i(release, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)
            can_i(security, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)
            can_i(bootstrap, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)

        for resource, name, namespace in rbac_objects:
            suffix = ("--namespace", namespace) if namespace else ()
            is_binding = resource.startswith("clusterrolebindings.") or resource.startswith("rolebindings.")
            scope_key = (
                "cluster"
                if not namespace
                else "state"
                if namespace == "fs2-system"
                else "gateway"
                if namespace == query["gateway_namespace"]
                else "controller"
            )
            bootstrap_binding_key = f"{scope_key}_bootstrap"
            for identity in (release, security, prior_security):
                for verb in ("create", "patch", "update", "delete"):
                    can_i(identity, context, "no", verb, resource, *suffix)
                    can_i(identity, context, "no", verb, resource, f"--resource-name={name}", *suffix)
                can_i(identity, context, "no", "deletecollection", resource, *suffix)
            for verb in ("create", "delete"):
                can_i(prior_bootstrap, context, "no", verb, resource, *suffix)
                can_i(prior_bootstrap, context, "no", verb, resource, f"--resource-name={name}", *suffix)
            can_i(prior_bootstrap, context, "no", "deletecollection", resource, *suffix)
            can_i(bootstrap, context, "no", "create", resource, *suffix)
            can_i(bootstrap, context, "no", "delete", resource, f"--resource-name={name}", *suffix)
            can_i(bootstrap, context, "no", "deletecollection", resource, *suffix)
            for verb in ("patch", "update"):
                can_i(bootstrap, context, "no", verb, resource, *suffix)
                can_i(
                    bootstrap,
                    context,
                    (
                        "yes" if is_binding else "no"
                    ),
                    verb,
                    resource,
                    f"--resource-name={name}",
                    *suffix,
                )
                can_i(prior_bootstrap, context, "no", verb, resource, *suffix)
                can_i(
                    prior_bootstrap,
                    context,
                    (
                        "yes"
                        if is_binding and rotation_binding_states[bootstrap_binding_key] == "before"
                        else "no"
                    ),
                    verb,
                    resource,
                    f"--resource-name={name}",
                    *suffix,
                )

        can_i(bootstrap, context, "yes", "list", "namespaces")
        can_i(bootstrap, context, "yes", "list", "serviceaccounts", "--all-namespaces")
        can_i(bootstrap, context, "yes", "list", "roles.rbac.authorization.k8s.io", "--all-namespaces")
        can_i(bootstrap, context, "yes", "list", "clusterroles.rbac.authorization.k8s.io")
        can_i(
            bootstrap,
            context,
            "yes",
            "list",
            "certificatesigningrequests.certificates.k8s.io",
        )
        service_accounts, roles, cluster_roles, csr_signers, kubernetes_subject_inventory_sha256 = (
            kubernetes_subject_inventory(bootstrap, context)
        )
        rule_impersonation_targets, rule_delegation_targets, rule_signers = (
            rbac_rule_authorization_targets(roles, cluster_roles)
        )
        identities = (release, security, bootstrap, prior_security, prior_bootstrap)
        impersonation_targets = tuple(
            [("users", name, "") for name in sorted(set(expected_principals.values()))]
        ) + (
            ("users", "fs2-network-policy-security-probe", ""),
            ("groups", "system:masters", ""),
            ("groups", "system:authenticated", ""),
            ("groups", "system:serviceaccounts", ""),
            ("groups", "system:serviceaccounts:fs2-system", ""),
            ("serviceaccounts", "fs2-network-policy-transition", "fs2-system"),
            ("uids.authentication.k8s.io", query["peer_uid"], ""),
            ("userextras.authentication.k8s.io", "scopes", ""),
        )
        impersonation_targets += tuple(("groups", group, "") for group in live_identity_groups)
        impersonation_targets += tuple(("users", user["username"], "") for user in provider_users)
        impersonation_targets += tuple(
            ("groups", group, "")
            for group in sorted(
                {
                    *provider_groups,
                    *(group for user in provider_users for group in user["groups"]),
                }
            )
        )
        impersonation_targets += tuple(
            ("serviceaccounts", account["name"], namespace)
            for namespace, accounts in service_accounts.items()
            for account in accounts
        )
        impersonation_targets += tuple(
            ("uids.authentication.k8s.io", uid, "") for uid in live_identity_uids
        )
        impersonation_targets += tuple(
            ("userextras.authentication.k8s.io", key, "") for key in live_identity_extra_keys
        )
        impersonation_targets += tuple(rule_impersonation_targets)
        impersonation_targets = tuple(sorted(set(impersonation_targets)))
        delegation_targets = set(
            ("clusterroles.rbac.authorization.k8s.io", role["name"], "") for role in cluster_roles
        ) | set(
            ("roles.rbac.authorization.k8s.io", role["name"], namespace)
            for namespace, namespace_roles in roles.items()
            for role in namespace_roles
        )
        delegation_targets = tuple(sorted(delegation_targets | rule_delegation_targets))
        signer_names = tuple(
            sorted(
                {
                    "kubernetes.io/kube-apiserver-client",
                    "kubernetes.io/kube-apiserver-client-kubelet",
                    "kubernetes.io/legacy-unknown",
                    *csr_signers,
                    *rule_signers,
                }
            )
        )
        for identity in identities:
            for resource in (
                "users",
                "groups",
                "serviceaccounts",
                "uids.authentication.k8s.io",
                "userextras.authentication.k8s.io",
            ):
                can_i(identity, context, "no", "impersonate", resource)
            for resource, name, namespace in impersonation_targets:
                arguments = ("impersonate", resource, f"--resource-name={name}")
                if namespace:
                    arguments += ("--namespace", namespace)
                can_i(identity, context, "no", *arguments)
            can_i(identity, context, "no", "create", "serviceaccounts", "--subresource=token", "--all-namespaces")
            for namespace, accounts in service_accounts.items():
                can_i(
                    identity,
                    context,
                    "no",
                    "create",
                    "serviceaccounts",
                    "--subresource=token",
                    "--namespace",
                    namespace,
                )
                for account in accounts:
                    can_i(
                        identity,
                        context,
                        "no",
                        "create",
                        "serviceaccounts",
                        f"--resource-name={account['name']}",
                        "--subresource=token",
                        "--namespace",
                        namespace,
                    )
            can_i(identity, context, "no", "create", "certificatesigningrequests.certificates.k8s.io")
            can_i(
                identity,
                context,
                "no",
                "create",
                "certificatesigningrequests.certificates.k8s.io",
                "--resource-name=fs2-network-policy-security-probe",
            )
            can_i(
                identity,
                context,
                "no",
                "update",
                "certificatesigningrequests.certificates.k8s.io",
                "--subresource=approval",
            )
            for signer in signer_names:
                for verb in ("approve", "sign"):
                    can_i(identity, context, "no", verb, "signers.certificates.k8s.io")
                    can_i(
                        identity,
                        context,
                        "no",
                        verb,
                        "signers.certificates.k8s.io",
                        f"--resource-name={signer}",
                    )
            for verb in ("bind", "escalate"):
                for resource in ("clusterroles.rbac.authorization.k8s.io", "roles.rbac.authorization.k8s.io"):
                    can_i(identity, context, "no", verb, resource)
                for resource, name, namespace in delegation_targets:
                    arguments = (verb, resource, f"--resource-name={name}")
                    if namespace:
                        arguments += ("--namespace", namespace)
                    can_i(identity, context, "no", *arguments)
            for namespace in service_accounts:
                for verb in ("update", "delete"):
                    can_i(identity, context, "no", verb, "namespaces", f"--resource-name={namespace}")
                can_i(identity, context, "no", "update", "namespaces/finalize", f"--resource-name={namespace}")

        can_i(bootstrap, context, "yes", "create", "subjectaccessreviews.authorization.k8s.io")
        preapply_retired_subjects = [
            {
                "username": value["username"],
                "uid": value["uid"],
                "groups": sorted(value["groups"]),
                "extra": {key: sorted(items) for key, items in sorted(value["extra"].items())},
            }
            for role, value in prior_expected.items()
            if role == "prior_bootstrap"
        ]
        for subject in [*humans, *preapply_retired_subjects]:
            for resource, name, namespace in namespaced:
                if resource.startswith("networkpolicies."):
                    group, short_resource = "networking.k8s.io", "networkpolicies"
                elif resource.startswith("leases."):
                    group, short_resource = "coordination.k8s.io", "leases"
                else:
                    group, short_resource = "", resource
                for verb in ("patch", "update", "delete"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group=group,
                        resource=short_resource,
                        namespace=namespace,
                        name=name,
                    )
            for resource, name, namespace in rbac_objects:
                group, short_resource = "rbac.authorization.k8s.io", resource.split(".", maxsplit=1)[0]
                for verb in ("create", "patch", "update", "delete"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group=group,
                        resource=short_resource,
                        namespace=namespace,
                    )
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group=group,
                        resource=short_resource,
                        namespace=namespace,
                        name=name,
                    )
            for resource in ("validatingadmissionpolicies", "validatingadmissionpolicybindings"):
                for verb in ("patch", "update", "delete"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group="admissionregistration.k8s.io",
                        resource=resource,
                        name="fs2-network-policy-boundary",
                    )
            for namespace in service_accounts:
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="update",
                    group="",
                    resource="namespaces",
                    name=namespace,
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="delete",
                    group="",
                    resource="namespaces",
                    name=namespace,
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="update",
                    group="",
                    resource="namespaces",
                    name=namespace,
                    subresource="finalize",
                )
            for namespace, accounts in service_accounts.items():
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="create",
                    group="",
                    resource="serviceaccounts",
                    namespace=namespace,
                    subresource="token",
                )
                for account in accounts:
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb="create",
                        group="",
                        resource="serviceaccounts",
                        namespace=namespace,
                        name=account["name"],
                        subresource="token",
                    )
            for group, resource in (
                ("", "users"),
                ("", "groups"),
                ("", "serviceaccounts"),
                ("authentication.k8s.io", "uids"),
                ("authentication.k8s.io", "userextras"),
            ):
                subject_denied(bootstrap, context, subject, verb="impersonate", group=group, resource=resource)
            for resource, name, namespace in impersonation_targets:
                if resource.endswith(".authentication.k8s.io"):
                    group, short_resource = "authentication.k8s.io", resource.split(".", maxsplit=1)[0]
                else:
                    group, short_resource = "", resource
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="impersonate",
                    group=group,
                    resource=short_resource,
                    namespace=namespace,
                    name=name,
                )
            subject_denied(
                bootstrap,
                context,
                subject,
                verb="create",
                group="certificates.k8s.io",
                resource="certificatesigningrequests",
            )
            subject_denied(
                bootstrap,
                context,
                subject,
                verb="create",
                group="certificates.k8s.io",
                resource="certificatesigningrequests",
                name="fs2-network-policy-security-probe",
            )
            subject_denied(
                bootstrap,
                context,
                subject,
                verb="update",
                group="certificates.k8s.io",
                resource="certificatesigningrequests",
                subresource="approval",
            )
            for signer in signer_names:
                for verb in ("approve", "sign"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group="certificates.k8s.io",
                        resource="signers",
                    )
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group="certificates.k8s.io",
                        resource="signers",
                        name=signer,
                    )
            for verb in ("bind", "escalate"):
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb=verb,
                    group="rbac.authorization.k8s.io",
                    resource="clusterroles",
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb=verb,
                    group="rbac.authorization.k8s.io",
                    resource="roles",
                    namespace="fs2-system",
                )
                for resource, name, namespace in delegation_targets:
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group="rbac.authorization.k8s.io",
                        resource=resource.split(".", maxsplit=1)[0],
                        namespace=namespace,
                        name=name,
                    )

        digest = hashlib.sha256(
            canonical(
                {
                    "query": query,
                    "credential_set_sha256": credential_set_sha256,
                    "provider_snapshot_sha256": provider_snapshot_sha256,
                    "provider_trust_anchor_sha256": provider_trust_anchor_sha256,
                    "provider_adapter_sha256": provider_adapter_sha256,
                    "kubernetes_subject_inventory_sha256": kubernetes_subject_inventory_sha256,
                    "auditor_bootstrap_sha256": auditor_bootstrap_sha256,
                    "external_role_bundle_sha256": external_role_bundle_sha256,
                    "rotation_phase": rotation_phase,
                    "rotation_binding_state_sha256": rotation_binding_state_sha256,
                }
            ).encode()
        ).hexdigest()
        print(
            canonical(
                {
                    "verified": "true",
                    "contract_sha256": digest,
                    "subject_inventory_sha256": inventory_sha256,
                    "provider_snapshot_sha256": provider_snapshot_sha256,
                    "provider_trust_anchor_sha256": provider_trust_anchor_sha256,
                    "provider_adapter_sha256": provider_adapter_sha256,
                    "kubernetes_subject_inventory_sha256": kubernetes_subject_inventory_sha256,
                    "auditor_bootstrap_sha256": auditor_bootstrap_sha256,
                    "external_role_bundle_sha256": external_role_bundle_sha256,
                    "rotation_phase": rotation_phase,
                    "rotation_binding_state_sha256": rotation_binding_state_sha256,
                    "credential_set_sha256": credential_set_sha256,
                    "release_kubeconfig_sha256": credential_hashes["release"],
                    "security_kubeconfig_sha256": credential_hashes["security"],
                    "bootstrap_kubeconfig_sha256": credential_hashes["bootstrap"],
                    "prior_security_kubeconfig_sha256": credential_hashes["prior_security"],
                    "prior_bootstrap_kubeconfig_sha256": credential_hashes["prior_bootstrap"],
                }
            )
        )
        return 0
    except (OSError, PreflightError, ValueError, json.JSONDecodeError) as error:
        print(f"network-policy security preflight failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
