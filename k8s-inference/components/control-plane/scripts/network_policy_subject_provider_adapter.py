#!/usr/bin/env python3
"""Capture the authoritative Nebius IAM human directory for external signing.

The adapter accepts no caller-supplied transcript. It reads a root-owned trust
anchor and a dedicated read-only CLI configuration from source-fixed paths,
executes the pinned bounded IAM queries itself, and emits a canonical unsigned
snapshot. External security automation signs that exact output.
"""

from __future__ import annotations

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

ADAPTER_ID = "fs2-serve.nebius.ai/nebius-iam-human-directory/v2"
SNAPSHOT_SCHEMA = "fs2-serve.nebius.ai/security-subject-provider-snapshot/v2"
TRUST_SCHEMA = "fs2-serve.nebius.ai/security-provider-trust-anchor/v2"
TRUST_ANCHOR_PATH = Path("/etc/fs2/security/network-policy-provider-trust-anchor-v2.json")
NEBIUS_CLI_PATH = Path("/usr/local/bin/nebius")
NEBIUS_CONFIG_PATH = Path("/etc/fs2/security/nebius-directory-reader.yaml")


class AdapterError(RuntimeError):
    """The authoritative provider enumeration is unavailable or incomplete."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _root_owned_file(
    path: Path,
    *,
    modes: set[int],
    maximum: int,
    label: str,
    required_gid: int | None = None,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or (required_gid is not None and metadata.st_gid != required_gid)
                or stat.S_IMODE(metadata.st_mode) not in modes
                or metadata.st_size > maximum
            ):
                raise AdapterError(f"{label} custody is not exact")
            value = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise AdapterError(f"{label} custody is unavailable") from error
    if len(value) > maximum:
        raise AdapterError(f"{label} exceeds its bound")
    return value


def _trust_anchor() -> tuple[dict[str, Any], str]:
    try:
        raw = _root_owned_file(
            TRUST_ANCHOR_PATH,
            modes={0o400, 0o444},
            maximum=65536,
            label="provider trust anchor",
            required_gid=0,
        )
        trust = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("provider trust anchor is invalid") from error
    expected = {
        "schema", "provider", "adapter", "directory_query", "tenant_sha256", "query_sha256",
        "snapshot_public_key", "snapshot_signer_key_id", "valid_from", "expires_at",
    }
    query_fields = {
        "cli_path", "config_path", "profile", "tenant_id", "page_size", "max_pages",
        "max_records", "timeout_seconds", "snapshot_ttl_seconds",
    }
    query = trust.get("directory_query", {}) if isinstance(trust, dict) else {}
    if (
        not isinstance(trust, dict)
        or set(trust) != expected
        or trust.get("schema") != TRUST_SCHEMA
        or trust.get("provider") != "nebius-iam"
        or not isinstance(query, dict)
        or set(query) != query_fields
        or query.get("cli_path") != str(NEBIUS_CLI_PATH)
        or query.get("config_path") != str(NEBIUS_CONFIG_PATH)
        or not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", str(query.get("profile", "")))
        or not re.fullmatch(r"tenant-[A-Za-z0-9-]{8,128}", str(query.get("tenant_id", "")))
        or not isinstance(query.get("page_size"), int)
        or not 1 <= query["page_size"] <= 1000
        or not isinstance(query.get("max_pages"), int)
        or not 1 <= query["max_pages"] <= 10000
        or not isinstance(query.get("max_records"), int)
        or not 1 <= query["max_records"] <= 100000
        or not isinstance(query.get("timeout_seconds"), int)
        or not 1 <= query["timeout_seconds"] <= 120
        or not isinstance(query.get("snapshot_ttl_seconds"), int)
        or not 300 <= query["snapshot_ttl_seconds"] <= 3600
        or trust.get("tenant_sha256") != hashlib.sha256(query["tenant_id"].encode()).hexdigest()
        or trust.get("query_sha256") != hashlib.sha256(canonical(query).encode()).hexdigest()
        or trust.get("adapter") != {
            "id": ADAPTER_ID,
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", str(trust.get("snapshot_public_key", "")))
        or trust.get("snapshot_signer_key_id")
        != hashlib.sha256(str(trust.get("snapshot_public_key", "")).encode()).hexdigest()
    ):
        raise AdapterError("provider trust anchor is not source-pinned exact")
    now = dt.datetime.now(dt.UTC)
    try:
        valid_from = dt.datetime.fromisoformat(str(trust["valid_from"]).replace("Z", "+00:00"))
        expires_at = dt.datetime.fromisoformat(str(trust["expires_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("provider trust validity is invalid") from error
    if (
        valid_from.tzinfo is None
        or expires_at.tzinfo is None
        or valid_from.astimezone(dt.UTC) > now
        or expires_at.astimezone(dt.UTC) <= now + dt.timedelta(seconds=query["snapshot_ttl_seconds"])
    ):
        raise AdapterError("provider trust anchor is not currently rollback-valid")
    _root_owned_file(
        NEBIUS_CONFIG_PATH,
        modes={0o400, 0o440},
        maximum=1048576,
        label="read-only provider CLI configuration",
    )
    return trust, hashlib.sha256(canonical(trust).encode()).hexdigest()


def _provider_page(query: dict[str, Any], command: list[str], token: str) -> dict[str, Any]:
    arguments = [
        str(NEBIUS_CLI_PATH), *command,
        "--page-size", str(query["page_size"]), "--page-token", token,
        "--format", "json", "--config", str(NEBIUS_CONFIG_PATH), "--profile", query["profile"],
        "--no-check-update", "--no-browser", "--color=false", "--retries", "1",
        "--timeout", f"{query['timeout_seconds']}s",
        "--auth-timeout", f"{query['timeout_seconds']}s",
    ]
    try:
        result = subprocess.run(  # noqa: S603 -- executable and arguments are source/root anchored
            arguments,
            capture_output=True,
            check=False,
            text=True,
            timeout=query["timeout_seconds"] + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError("provider query did not complete within its bound") from error
    if result.returncode != 0 or result.stderr.strip():
        raise AdapterError("provider query failed closed")
    try:
        page = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError("provider query returned invalid JSON") from error
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("items"), list)
        or not isinstance(page.get("next_page_token", ""), str)
        or set(page) - {"items", "next_page_token"}
    ):
        raise AdapterError("provider page schema is not exact")
    return page


def _list_pages(
    query: dict[str, Any], command: list[str], operation: str, budgets: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    token = ""
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    while True:
        budgets["pages"] -= 1
        if budgets["pages"] < 0:
            raise AdapterError("provider enumeration exceeded its page bound")
        page = _provider_page(query, command, token)
        budgets["records"] -= len(page["items"])
        if budgets["records"] < 0 or len(page["items"]) > query["page_size"]:
            raise AdapterError("provider enumeration exceeded its record bound")
        pages.append({
            "operation": operation,
            "request_token": token,
            "response": page,
            "next_token": page.get("next_page_token", ""),
        })
        items.extend(page["items"])
        next_token = page.get("next_page_token", "")
        if not next_token:
            return items, pages
        if next_token == token:
            raise AdapterError("provider pagination did not advance")
        token = next_token


def _metadata_identity(value: Any, *, label: str) -> tuple[str, str]:
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
    identifier = metadata.get("id") if isinstance(metadata, dict) else None
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if (
        not isinstance(identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,253}", identifier)
        or not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", name)
    ):
        raise AdapterError(f"provider {label} identity is invalid")
    return identifier, name


def capture() -> dict[str, Any]:
    trust, trust_sha256 = _trust_anchor()
    query = trust["directory_query"]
    budgets = {"pages": query["max_pages"], "records": query["max_records"]}
    raw_pages: list[dict[str, Any]] = []
    user_items, pages = _list_pages(
        query,
        ["iam", "tenant-user-account-with-attributes", "list", "--parent-id", query["tenant_id"]],
        "tenant-user-account-with-attributes.list",
        budgets,
    )
    raw_pages.extend(pages)
    group_items, pages = _list_pages(
        query,
        ["iam", "group", "list", "--parent-id", query["tenant_id"]],
        "group.list",
        budgets,
    )
    raw_pages.extend(pages)

    users_by_id: dict[str, dict[str, Any]] = {}
    for item in user_items:
        account = item.get("tenant_user_account", {}) if isinstance(item, dict) else {}
        identifier, _ = _metadata_identity(account, label="tenant user")
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        username = attributes.get("email") if isinstance(attributes, dict) else None
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9:@._+/-]{3,253}", username)
            or identifier in users_by_id
            or any(value["username"] == username for value in users_by_id.values())
        ):
            raise AdapterError("provider human account inventory is invalid or duplicated")
        users_by_id[identifier] = {"username": username, "groups": []}

    groups_by_id: dict[str, str] = {}
    for item in group_items:
        identifier, name = _metadata_identity(item, label="group")
        if identifier in groups_by_id or name in groups_by_id.values():
            raise AdapterError("provider group inventory is duplicated")
        groups_by_id[identifier] = name

    for user_id, user in sorted(users_by_id.items()):
        membership_items, pages = _list_pages(
            query,
            ["iam", "group-membership", "list-member-of", "--subject-id", user_id],
            f"group-membership.list-member-of:{hashlib.sha256(user_id.encode()).hexdigest()}",
            budgets,
        )
        raw_pages.extend(pages)
        memberships: list[str] = []
        for membership in membership_items:
            metadata = membership.get("metadata", {}) if isinstance(membership, dict) else {}
            group_id = metadata.get("parent_id") if isinstance(metadata, dict) else None
            spec = membership.get("spec", {}) if isinstance(membership, dict) else {}
            if spec.get("member_id") != user_id or group_id not in groups_by_id:
                raise AdapterError("provider group membership is outside the complete directory inventory")
            memberships.append(groups_by_id[group_id])
        if len(memberships) != len(set(memberships)):
            raise AdapterError("provider group membership is duplicated")
        user["groups"] = sorted(memberships)

    if not users_by_id or not groups_by_id or not raw_pages:
        raise AdapterError("provider human directory is empty")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    logical_cursor = ""
    receipts: list[dict[str, Any]] = []
    for index, page in enumerate(raw_pages):
        next_logical_cursor = (
            "" if index + 1 == len(raw_pages) else canonical({
                "operation": raw_pages[index + 1]["operation"],
                "request_token": raw_pages[index + 1]["request_token"],
            })
        )
        receipts.append({
            "index": index,
            "request_cursor_sha256": hashlib.sha256(logical_cursor.encode()).hexdigest(),
            "response_sha256": hashlib.sha256(
                canonical({"operation": page["operation"], "response": page["response"]}).encode()
            ).hexdigest(),
            "next_cursor_sha256": (
                hashlib.sha256(next_logical_cursor.encode()).hexdigest() if next_logical_cursor else ""
            ),
        })
        logical_cursor = next_logical_cursor
    transcript_sha256 = hashlib.sha256(canonical(raw_pages).encode()).hexdigest()
    users = sorted(users_by_id.values(), key=lambda item: item["username"])
    groups = sorted(groups_by_id.values())
    record_count = query["max_records"] - budgets["records"]
    if record_count < len(users) + len(groups):
        raise AdapterError("provider enumeration record count is incomplete")
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": f"nebius-iam-{now.strftime('%Y%m%dT%H%M%SZ')}-{transcript_sha256[:16]}",
        "provider": "nebius-iam",
        "adapter": {"id": ADAPTER_ID, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "trust_anchor_sha256": trust_sha256,
        "tenant_sha256": trust["tenant_sha256"],
        "query_sha256": trust["query_sha256"],
        "complete": True,
        "pagination": {
            "page_size": query["page_size"], "page_count": len(receipts),
            "record_count": record_count,
            "subject_count": len(users) + len(groups),
            "terminal_cursor": "", "pages": receipts,
        },
        "human_users": users,
        "human_groups": groups,
        "captured_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(seconds=query["snapshot_ttl_seconds"])).isoformat(),
        "signer_key_id": trust["snapshot_signer_key_id"],
    }


def main() -> int:
    if len(sys.argv) != 1:
        print("provider adapter failed closed: caller arguments are forbidden", file=sys.stderr)
        return 1
    try:
        print(canonical(capture()))
        return 0
    except (AdapterError, OSError, TypeError, ValueError) as error:
        print(f"provider adapter failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
