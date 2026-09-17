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


class PreflightError(RuntimeError):
    """The plan-time security boundary is not exact."""


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
            or not re.fullmatch(r"[A-Za-z0-9:@._/-]{3,253}", str(subject.get("username", "")))
            or subject.get("username") in forbidden_usernames
            or not isinstance(subject.get("groups"), list)
            or not subject["groups"]
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


def verified_provider_snapshot(
    raw_snapshot: str,
    provider_public_key: str,
    *,
    expected_tenant_sha256: str,
    expected_query_sha256: str,
    rollback_valid_until: int,
    forbidden_usernames: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], str]:
    signed = verified_envelope(raw_snapshot, provider_public_key, label="provider/IAM subject snapshot")
    expected_fields = {
        "schema",
        "snapshot_id",
        "provider",
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
        signed.get("schema") != "fs2-serve.nebius.ai/security-subject-provider-snapshot/v1"
        or signed.get("complete") is not True
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(signed.get("snapshot_id", "")))
        or not re.fullmatch(r"[a-z][a-z0-9.-]{2,63}", str(signed.get("provider", "")))
        or signed.get("tenant_sha256") != expected_tenant_sha256
        or signed.get("query_sha256") != expected_query_sha256
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
        or pagination.get("record_count") != len(users) + len(groups)
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
) -> list[dict[str, str]]:
    """Read one exact List snapshot with bounded Kubernetes pagination."""
    cursor = ""
    snapshot_resource_version = ""
    result: list[dict[str, str]] = []
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
            result.append({"name": name, "uid": uid, "resourceVersion": resource_version})
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
) -> tuple[dict[str, list[dict[str, str]]], str]:
    """Enumerate every namespace-local ServiceAccount twice and fail on drift."""

    def discover() -> dict[str, list[dict[str, str]]]:
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
        inventory: dict[str, list[dict[str, str]]] = {}
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
            inventory[name] = sorted(service_accounts, key=lambda value: value["name"])
        return inventory

    first = discover()
    second = discover()
    if first != second:
        raise PreflightError("Kubernetes subject inventory drifted during authorization proof")
    digest = hashlib.sha256(canonical(first).encode()).hexdigest()
    return first, digest


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
        "provider_public_key",
        "provider_tenant_sha256",
        "provider_query_sha256",
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
        live_identity_uids: list[str] = []
        live_identity_groups: list[str] = []
        live_identity_extra_keys: list[str] = []
        if query["mode"] == "public":
            exact_file(provider_snapshot_path, label="provider/IAM subject snapshot")
            if any(paths[role].parent.name != query["identity_epoch"] for role in ("release", "security", "bootstrap")):
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
            if query["provider_public_key"] == query["recovery_public_key"]:
                raise PreflightError("provider/IAM and recovery inventory authorities must be disjoint")
            provider_signed, provider_users, provider_groups, provider_snapshot_sha256 = verified_provider_snapshot(
                provider_snapshot_path.read_text(encoding="utf-8"),
                query["provider_public_key"],
                expected_tenant_sha256=query["provider_tenant_sha256"],
                expected_query_sha256=query["provider_query_sha256"],
                rollback_valid_until=rollback_valid_until,
                forbidden_usernames=set(expected_principals.values()),
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
            inventory_sha256 = hashlib.sha256(b"internal-only").hexdigest()
            provider_snapshot_sha256 = hashlib.sha256(b"internal-only-provider").hexdigest()

        release = paths["release"]
        security = paths["security"]
        bootstrap = paths["bootstrap"]
        prior_security = paths["prior_security"]
        prior_bootstrap = paths["prior_bootstrap"]
        context = query["context"]
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
                can_i(bootstrap, context, "yes", verb, resource, "--resource-name=fs2-network-policy-boundary")
                can_i(prior_security, context, "no", verb, resource, "--resource-name=fs2-network-policy-boundary")
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
            ("roles.rbac.authorization.k8s.io", "fs2-network-policy-transition", "fs2-system"),
            ("rolebindings.rbac.authorization.k8s.io", "fs2-network-policy-transition", "fs2-system"),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway",
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
        )
        for resource, name, namespace in namespaced:
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
                can_i(bootstrap, context, "yes", verb, resource, f"--resource-name={name}", "--namespace", namespace)
                can_i(
                    prior_security,
                    context,
                    "no",
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
            for identity in (release, security, prior_security, prior_bootstrap):
                for verb in ("create", "patch", "update", "delete"):
                    can_i(identity, context, "no", verb, resource, *suffix)
                    can_i(identity, context, "no", verb, resource, f"--resource-name={name}", *suffix)
                can_i(identity, context, "no", "deletecollection", resource, *suffix)
            can_i(bootstrap, context, "no", "create", resource, *suffix)
            can_i(bootstrap, context, "no", "delete", resource, f"--resource-name={name}", *suffix)
            can_i(bootstrap, context, "no", "deletecollection", resource, *suffix)
            for verb in ("patch", "update"):
                can_i(bootstrap, context, "no", verb, resource, *suffix)
                can_i(bootstrap, context, "yes", verb, resource, f"--resource-name={name}", *suffix)

        can_i(bootstrap, context, "yes", "list", "namespaces")
        can_i(bootstrap, context, "yes", "list", "serviceaccounts", "--all-namespaces")
        service_accounts, kubernetes_subject_inventory_sha256 = kubernetes_subject_inventory(bootstrap, context)

        identities = (release, security, bootstrap, prior_security, prior_bootstrap)
        impersonation_targets = tuple(
            [("users", name) for name in sorted(set(expected_principals.values()))]
        ) + (
            ("users", "fs2-network-policy-security-probe"),
            ("groups", "system:masters"),
            ("groups", "system:authenticated"),
            ("groups", "system:serviceaccounts"),
            ("groups", "system:serviceaccounts:fs2-system"),
            ("serviceaccounts", "system:serviceaccount:fs2-system:fs2-network-policy-transition"),
            ("uids.authentication.k8s.io", query["peer_uid"]),
            ("userextras.authentication.k8s.io", "scopes"),
        )
        impersonation_targets += tuple(("groups", group) for group in live_identity_groups)
        impersonation_targets += tuple(
            ("serviceaccounts", f"{namespace}:{account['name']}")
            for namespace, accounts in service_accounts.items()
            for account in accounts
        )
        impersonation_targets += tuple(("uids.authentication.k8s.io", uid) for uid in live_identity_uids)
        impersonation_targets += tuple(
            ("userextras.authentication.k8s.io", key) for key in live_identity_extra_keys
        )
        impersonation_targets = tuple(sorted(set(impersonation_targets)))
        delegation_targets = (
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-owner", ""),
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-auditor", ""),
            ("roles.rbac.authorization.k8s.io", "fs2-network-policy-transition", "fs2-system"),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-gateway",
                query["gateway_namespace"],
            ),
            (
                "roles.rbac.authorization.k8s.io",
                "fs2-network-policy-transition-controller",
                query["controller_namespace"],
            ),
        )
        signer_names = (
            "kubernetes.io/kube-apiserver-client",
            "kubernetes.io/kube-apiserver-client-kubelet",
            "kubernetes.io/legacy-unknown",
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
            for resource, name in impersonation_targets:
                can_i(identity, context, "no", "impersonate", resource, f"--resource-name={name}")
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
        retired_subjects = [
            {
                "username": value["username"],
                "uid": value["uid"],
                "groups": sorted(value["groups"]),
                "extra": {key: sorted(items) for key, items in sorted(value["extra"].items())},
            }
            for value in prior_expected.values()
        ]
        for subject in [*humans, *retired_subjects]:
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
            for resource, name in impersonation_targets:
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
                    "kubernetes_subject_inventory_sha256": kubernetes_subject_inventory_sha256,
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
                    "kubernetes_subject_inventory_sha256": kubernetes_subject_inventory_sha256,
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
