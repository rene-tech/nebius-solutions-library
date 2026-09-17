#!/usr/bin/env python3
"""Normalize the authoritative Nebius IAM human-directory export for signing.

External security automation owns provider authentication and the pinned signing
key. This source-fixed adapter accepts only the complete, bounded provider page
transcript on stdin and emits one canonical unsigned snapshot payload on stdout.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ADAPTER_ID = "fs2-serve.nebius.ai/nebius-iam-human-directory/v1"
RAW_SCHEMA = "fs2-serve.nebius.ai/nebius-iam-human-directory-pages/v1"
SNAPSHOT_SCHEMA = "fs2-serve.nebius.ai/security-subject-provider-snapshot/v1"


class AdapterError(RuntimeError):
    """The provider export is not the exact authoritative page transcript."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def subjects(value: Any, *, allow_empty: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, dict) or set(value) != {"human_users", "human_groups"}:
        raise AdapterError("provider subjects are not exact")
    users = value["human_users"]
    groups = value["human_groups"]
    if (
        not isinstance(users, list)
        or (not allow_empty and not users)
        or not isinstance(groups, list)
        or (not allow_empty and not groups)
    ):
        raise AdapterError("provider subjects are empty")
    normalized_users = []
    for user in users:
        if (
            not isinstance(user, dict)
            or set(user) != {"username", "groups"}
            or not re.fullmatch(r"[A-Za-z0-9:@._/-]{3,253}", str(user.get("username", "")))
            or not isinstance(user.get("groups"), list)
            or not user["groups"]
            or any(
                not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group)
                for group in user["groups"]
            )
            or len(user["groups"]) != len(set(user["groups"]))
        ):
            raise AdapterError("provider user is invalid")
        normalized_users.append({"username": user["username"], "groups": sorted(set(user["groups"]))})
    normalized_users.sort(key=lambda item: item["username"])
    if (
        any(
            not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group)
            for group in groups
        )
    ):
        raise AdapterError("provider group is invalid")
    normalized_groups = sorted(set(groups))
    if len(normalized_users) != len(users) or len(normalized_groups) != len(groups):
        raise AdapterError("provider subjects contain duplicates")
    return normalized_users, normalized_groups


def normalize(raw: Any) -> dict[str, Any]:
    expected = {
        "schema",
        "snapshot_id",
        "provider",
        "tenant_sha256",
        "query_sha256",
        "page_size",
        "pages",
        "captured_at",
        "expires_at",
        "signer_key_id",
        "trust_anchor_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise AdapterError("provider export fields are not exact")
    pages = raw["pages"]
    page_size = raw["page_size"]
    if (
        raw["schema"] != RAW_SCHEMA
        or raw["provider"] != "nebius-iam"
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
        or not isinstance(pages, list)
        or not 1 <= len(pages) <= 10000
    ):
        raise AdapterError("provider export contract is invalid")
    for field in ("tenant_sha256", "query_sha256", "trust_anchor_sha256", "signer_key_id"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(raw.get(field, ""))):
            raise AdapterError("provider export identity is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(raw.get("snapshot_id", ""))):
        raise AdapterError("provider snapshot identity is invalid")
    try:
        captured = dt.datetime.fromisoformat(str(raw["captured_at"]).replace("Z", "+00:00"))
        expires = dt.datetime.fromisoformat(str(raw["expires_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("provider export validity is invalid") from error
    if captured.tzinfo is None or expires.tzinfo is None or expires <= captured:
        raise AdapterError("provider export validity is invalid")
    all_users: list[dict[str, Any]] = []
    all_groups: list[str] = []
    expected_cursor = ""
    receipts = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or set(page) != {"request_cursor", "response", "next_cursor"}:
            raise AdapterError("provider page is not exact")
        if page["request_cursor"] != expected_cursor or not isinstance(page["next_cursor"], str):
            raise AdapterError("provider cursor chain is discontinuous")
        page_users, page_groups = subjects(page["response"], allow_empty=True)
        if len(page_users) + len(page_groups) > page_size:
            raise AdapterError("provider page exceeds the pinned page size")
        all_users.extend(page_users)
        all_groups.extend(page_groups)
        receipts.append(
            {
                "index": index,
                "request_cursor_sha256": hashlib.sha256(page["request_cursor"].encode()).hexdigest(),
                "response_sha256": hashlib.sha256(canonical(page["response"]).encode()).hexdigest(),
                "next_cursor_sha256": (
                    hashlib.sha256(page["next_cursor"].encode()).hexdigest() if page["next_cursor"] else ""
                ),
            }
        )
        expected_cursor = page["next_cursor"]
        if not expected_cursor and index + 1 != len(pages):
            raise AdapterError("provider transcript continues after its terminal cursor")
    if expected_cursor:
        raise AdapterError("provider transcript has no terminal cursor")
    users = sorted(all_users, key=lambda item: item["username"])
    groups = sorted(all_groups)
    if (
        not users
        or not groups
        or len({item["username"] for item in users}) != len(users)
        or len(set(groups)) != len(groups)
        or any(set(item["groups"]) - set(groups) for item in users)
    ):
        raise AdapterError("provider transcript subjects are empty or duplicated across pages")
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": raw["snapshot_id"],
        "provider": raw["provider"],
        "adapter": {"id": ADAPTER_ID, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "trust_anchor_sha256": raw["trust_anchor_sha256"],
        "tenant_sha256": raw["tenant_sha256"],
        "query_sha256": raw["query_sha256"],
        "complete": True,
        "pagination": {
            "page_size": page_size,
            "page_count": len(receipts),
            "record_count": len(users) + len(groups),
            "terminal_cursor": "",
            "pages": receipts,
        },
        "human_users": users,
        "human_groups": groups,
        "captured_at": raw["captured_at"],
        "expires_at": raw["expires_at"],
        "signer_key_id": raw["signer_key_id"],
    }


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        print(canonical(normalize(raw)))
        return 0
    except (AdapterError, json.JSONDecodeError, OSError, TypeError) as error:
        print(f"provider adapter failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
