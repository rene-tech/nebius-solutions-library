#!/usr/bin/env python3
"""Fail closed unless the Nebius profile is the exact narrow security owner."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from verify_authority_ledger import REGISTRY_PATH, canonical, safe_root_read, strict_json

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_KEY_LIFETIME = timedelta(days=90)


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


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("authority public-key expiry is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("authority public-key expiry lacks a timezone")
    return parsed.astimezone(UTC)


def verify(profile: str) -> dict[str, str]:
    if not profile or profile in {"default", "sandbox"}:
        raise ValueError("a dedicated named provider-security profile is required")
    registry = strict_json(safe_root_read(REGISTRY_PATH), "authority registry")
    whoami = _cli(profile, "iam", "whoami")
    try:
        identity = whoami["service_account_profile"]["info"]
        identity_id = identity["metadata"]["id"]
        active = identity["status"]["active"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Nebius profile is not a service-account identity") from exc
    if identity_id != registry["authority_service_account_id"] or active is not True:
        raise ValueError("Nebius provider profile is not the active external authority")

    memberships = _items(
        _cli(
            profile,
            "iam",
            "group-membership",
            "list-members",
            "--parent-id",
            registry["authority_group_id"],
            "--page-size",
            "1000",
        ),
        "memberships",
    )
    members = sorted(item.get("spec", {}).get("member_id") for item in memberships)
    if members != [registry["authority_service_account_id"]]:
        raise ValueError("external authority group membership is not exact and singleton")

    permits = _items(
        _cli(
            profile,
            "iam",
            "access-permit",
            "list",
            "--parent-id",
            registry["authority_group_id"],
            "--page-size",
            "1000",
        ),
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

    public_keys = _items(
        _cli(
            profile,
            "iam",
            "auth-public-key",
            "list",
            "--parent-id",
            registry["authority_service_account_id"],
            "--page-size",
            "1000",
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

    projection = {
        "identity_id": identity_id,
        "group_id": registry["authority_group_id"],
        "permits": observed_permits,
        "public_keys": observed_keys,
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
