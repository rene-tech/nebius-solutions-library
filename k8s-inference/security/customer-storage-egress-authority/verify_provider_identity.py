#!/usr/bin/env python3
"""Fail closed unless the Nebius profile is the exact narrow security owner."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from pathlib import Path

from verify_authority_ledger import REGISTRY_PATH, canonical, safe_root_read, strict_json

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_KEY_LIFETIME = timedelta(days=90)
AUTHORITY_GRAPH_ADAPTER = Path(
    "/usr/libexec/fs2-security/provider-effective-authority"
)
REJECTED_SAI10_COMMIT = "1ae009b858924138de70932ac84b8e595a2656a1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _open_fixed_adapter(path: Path) -> int:
    parts = path.parts[1:]
    if (
        not path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("provider authority adapter path is invalid")
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
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)


def _cli(profile: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - fixed executable and bounded arguments.
        [
            "nebius",
            *arguments,
            "--profile",
            profile,
            "--no-browser",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0 or len(result.stdout.encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("Nebius provider identity preflight failed")
    value = strict_json(result.stdout.encode(), "Nebius provider identity response")
    if not isinstance(value, dict):
        raise ValueError("Nebius provider identity response is not an object")
    return value


def _items(value: dict[str, Any], field: str) -> list[dict[str, Any]]:
    items = value.get(field)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"Nebius {field} response is malformed")
    return items


def _list_all(
    profile: str,
    command: tuple[str, ...],
    field: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(1000):
        arguments = [*command, "--page-size", "1000"]
        if page_token:
            arguments.extend(("--page-token", page_token))
        response = _cli(profile, *arguments)
        result.extend(_items(response, field))
        next_token = response.get("next_page_token", "")
        if not isinstance(next_token, str):
            raise ValueError(f"Nebius {field} pagination token is malformed")
        if not next_token:
            return result
        if next_token in seen_tokens:
            raise ValueError(f"Nebius {field} pagination repeated a token")
        seen_tokens.add(next_token)
        page_token = next_token
    raise ValueError(f"Nebius {field} inventory exceeded the page bound")


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("authority public-key expiry is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("authority public-key expiry lacks a timezone")
    return parsed.astimezone(UTC)


def _provider_authority_graph(
    profile: str, project_id: str, cluster_id: str, expected_sha256: str
) -> dict[str, Any]:
    """Run the separately custodied provider-native effective-authority adapter."""

    descriptor = _open_fixed_adapter(AUTHORITY_GRAPH_ADAPTER)
    try:
        metadata = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or not metadata.st_mode & 0o111
            or metadata.st_size <= 0
            or metadata.st_size > MAX_OUTPUT_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        ):
            raise ValueError("provider authority adapter custody differs")
        payload = os.read(descriptor, metadata.st_size + 1)
        if (
            len(payload) != metadata.st_size
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise ValueError("provider authority adapter custody differs")
        result = subprocess.run(  # noqa: S603 - descriptor-bound approved adapter.
            [
                f"/proc/self/fd/{descriptor}",
                "--profile",
                profile,
                "--project-id",
                project_id,
                "--cluster-id",
                cluster_id,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            pass_fds=(descriptor,),
        )
        after = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or result.returncode != 0
            or len(result.stdout.encode()) > MAX_OUTPUT_BYTES
        ):
            raise ValueError("provider authority adapter failed or changed during use")
        value = strict_json(result.stdout.encode(), "provider authority adapter response")
        return value
    finally:
        os.close(descriptor)


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode not in {0, 1}:
        raise ValueError("Git ancestry verification failed")
    return result.returncode == 0


def _metadata_projection(item: dict[str, Any]) -> dict[str, str]:
    metadata = item.get("metadata", {})
    identifier = metadata.get("id")
    name = metadata.get("name")
    if not isinstance(identifier, str) or not identifier or not isinstance(name, str):
        raise ValueError("Nebius IAM resource metadata is incomplete")
    return {"id": identifier, "name": name}


def _project_inventory(
    profile: str,
    project_id: str,
    effective_principal_ids: set[str],
) -> dict[str, Any]:
    """Project every IAM object and grant without reading credential material."""

    groups = _list_all(
        profile,
        ("iam", "group", "list", "--parent-id", project_id),
        "groups",
    )
    service_accounts = _list_all(
        profile,
        ("iam", "service-account", "list", "--parent-id", project_id),
        "service_accounts",
    )
    group_rows: list[dict[str, Any]] = []
    # Seed from the separately signed provider-native effective-authority
    # graph, never from candidate declarations. This includes inherited,
    # federated and external principals that are absent from project-local
    # service-account/group listings.
    all_principals = set(effective_principal_ids)
    for source in groups:
        group = _metadata_projection(source)
        all_principals.add(group["id"])
        memberships = _list_all(
            profile,
            (
                "iam",
                "group-membership",
                "list-members",
                "--parent-id",
                group["id"],
            ),
            "memberships",
        )
        members = sorted(
            {
                str(item.get("spec", {}).get("member_id"))
                for item in memberships
                if item.get("spec", {}).get("member_id")
            }
        )
        if len(members) != len(memberships):
            raise ValueError("Nebius group membership inventory is incomplete or duplicated")
        all_principals.update(members)
        group_rows.append({**group, "members": members})

    service_account_rows: list[dict[str, Any]] = []
    for source in service_accounts:
        account = _metadata_projection(source)
        all_principals.add(account["id"])
        keys = _list_all(
            profile,
            ("iam", "auth-public-key", "list", "--parent-id", account["id"]),
            "auth_public_keys",
        )
        key_rows = []
        for item in keys:
            key_id = item.get("metadata", {}).get("id")
            expires_at = item.get("spec", {}).get("expires_at")
            status = item.get("status", {})
            state = "ACTIVE" if status.get("active") is True else str(status.get("state", ""))
            if not isinstance(key_id, str) or not key_id or not isinstance(expires_at, str):
                raise ValueError("Nebius public-key inventory is incomplete")
            key_rows.append({"id": key_id, "expires_at": expires_at, "state": state})
        service_account_rows.append(
            {
                **account,
                "active": source.get("status", {}).get("active") is True,
                "auth_public_keys": sorted(key_rows, key=lambda item: item["id"]),
            }
        )

    permit_rows: list[dict[str, str]] = []
    for principal_id in sorted(all_principals):
        permits = _list_all(
            profile,
            ("iam", "access-permit", "list", "--parent-id", principal_id),
            "access_permits",
        )
        for item in permits:
            row = {
                "parent_id": principal_id,
                "id": str(item.get("metadata", {}).get("id", "")),
                "role": str(item.get("spec", {}).get("role", "")),
                "resource_id": str(item.get("spec", {}).get("resource_id", "")),
            }
            if any(not value for value in row.values()):
                raise ValueError("Nebius access-permit inventory is incomplete")
            permit_rows.append(row)

    return {
        "groups": sorted(group_rows, key=lambda item: item["id"]),
        "service_accounts": sorted(service_account_rows, key=lambda item: item["id"]),
        "access_permits": sorted(permit_rows, key=lambda item: (item["parent_id"], item["id"])),
    }


def verify(profile: str) -> dict[str, str]:
    if not profile or profile in {"default", "sandbox"}:
        raise ValueError("a dedicated named provider-security profile is required")
    registry = strict_json(safe_root_read(REGISTRY_PATH), "authority registry")
    accepted_commit = registry.get("accepted_custody", {}).get("sai10_commit", "")
    if (
        not isinstance(accepted_commit, str)
        or len(accepted_commit) != 40
        or _is_ancestor(REJECTED_SAI10_COMMIT, accepted_commit)
        or _is_ancestor(REJECTED_SAI10_COMMIT, "HEAD")
    ):
        raise ValueError("provider authority requires clean accepted SAI-10 ancestry")
    identity_inventory = registry.get("kubernetes_identity_inventory")
    if not isinstance(identity_inventory, list):
        raise ValueError("provider-bound Kubernetes identity inventory is absent")
    whoami = _cli(profile, "iam", "whoami")
    try:
        identity = whoami["service_account_profile"]["info"]
        identity_id = identity["metadata"]["id"]
        active = identity["status"]["active"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Nebius profile is not a service-account identity") from exc
    if identity_id != registry["authority_service_account_id"] or active is not True:
        raise ValueError("Nebius provider profile is not the active external authority")

    memberships = _list_all(
        profile,
        (
            "iam",
            "group-membership",
            "list-members",
            "--parent-id",
            registry["authority_group_id"],
        ),
        "memberships",
    )
    members = sorted(item.get("spec", {}).get("member_id") for item in memberships)
    if members != [registry["authority_service_account_id"]]:
        raise ValueError("external authority group membership is not exact and singleton")

    permits = _list_all(
        profile,
        ("iam", "access-permit", "list", "--parent-id", registry["authority_group_id"]),
        "access_permits",
    )
    observed_permits = [
        {
            "id": item.get("metadata", {}).get("id"),
            "role": item.get("spec", {}).get("role"),
            "resource_id": item.get("spec", {}).get("resource_id"),
        }
        for item in permits
    ]
    expected_permits = sorted(
        registry["authority_access_permits"], key=lambda item: item["id"]
    )
    observed_permits.sort(key=lambda item: str(item["id"]))
    if observed_permits != expected_permits or any(
        isinstance(item["role"], str)
        and (item["role"] == "admin" or item["role"].endswith(".admin"))
        for item in observed_permits
    ):
        raise ValueError("external authority permits differ or include admin")

    public_keys = _list_all(
        profile,
        (
            "iam",
            "auth-public-key",
            "list",
            "--parent-id",
            registry["authority_service_account_id"],
        ),
        "auth_public_keys",
    )
    observed_keys = sorted(
        (
            {
                "id": item.get("metadata", {}).get("id"),
                "expires_at": item.get("spec", {}).get("expires_at"),
            }
            for item in public_keys
        ),
        key=lambda item: str(item["id"]),
    )
    expected_keys = sorted(
        registry["authority_auth_public_keys"], key=lambda item: item["id"]
    )
    now = datetime.now(UTC)
    if observed_keys != expected_keys:
        raise ValueError("external authority public-key inventory differs")
    for source in public_keys:
        status = source.get("status", {})
        if status.get("active") is not True and status.get("state") != "ACTIVE":
            raise ValueError("external authority public key is not active")
    for item in observed_keys:
        expires_at = _timestamp(item["expires_at"])
        if expires_at <= now or expires_at > now + MAX_KEY_LIFETIME:
            raise ValueError("external authority public key is expired or overlong")

    receipt = registry.get("provider_project_iam_inventory_receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("inventory"), dict):
        raise ValueError("signed provider project IAM inventory is absent")
    authority_graph = registry.get("provider_effective_authority_graph_receipt")
    graph_principals = (
        authority_graph.get("principals")
        if isinstance(authority_graph, dict)
        else None
    )
    if not isinstance(graph_principals, list) or not graph_principals:
        raise ValueError("provider-native effective authority graph is absent")
    live_authority_graph = _provider_authority_graph(
        profile,
        registry["authority_project_id"],
        authority_graph["cluster_id"],
        registry["provider_authority_adapter_sha256"],
    )
    signed_graph_body = {
        key: value
        for key, value in authority_graph.items()
        if key not in {"payload_sha256", "signature", "observed_at"}
    }
    live_graph_body = {
        key: value for key, value in live_authority_graph.items() if key != "observed_at"
    }
    if live_graph_body != signed_graph_body:
        raise ValueError("live provider-native authority graph differs from signed custody")
    provider_principal_ids = {
        str(item.get("id"))
        for item in graph_principals
        if isinstance(item, dict) and item.get("id")
    }
    if len(provider_principal_ids) != len(graph_principals):
        raise ValueError("provider-native effective authority graph contains duplicate identities")
    observed_project_inventory = _project_inventory(
        profile,
        registry["authority_project_id"],
        provider_principal_ids,
    )
    if observed_project_inventory != receipt["inventory"]:
        raise ValueError("live provider project IAM inventory differs from the signed receipt")

    projection = {
        "identity_id": identity_id,
        "group_id": registry["authority_group_id"],
        "permits": observed_permits,
        "public_keys": observed_keys,
        "project_inventory_sha256": hashlib.sha256(
            canonical(observed_project_inventory)
        ).hexdigest(),
        "provider_authority_graph_sha256": hashlib.sha256(
            canonical(live_graph_body)
        ).hexdigest(),
    }
    return {
        "authorized": "true",
        "provider_identity_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {"profile"}:
            raise ValueError("provider identity query differs")
        print(json.dumps(verify(query["profile"]), sort_keys=True))
        return 0
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"customer-storage provider identity rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
