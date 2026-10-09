#!/usr/bin/env python3
"""Operator CLI for existing Scientific AI user/storage APIs. Standard library only.

Mutations require --apply. PostgreSQL/the platform remain the identity and
storage authority; the optional policy is operator intent and naming aliases.
Retirement disables access and retains data/accounting, not a bucket purge.
"""

from __future__ import annotations

import argparse
import hashlib
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class LifecycleError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LifecycleError("API redirect refused; use the exact trusted API origin")


def read_json(path):
    return json.loads(Path(path).read_text())


def private_output(path, value):
    """Never overwrite a handover, follow a symlink, or print a secret."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def load_policy(path):
    if not path:
        return {"tenants": {}}
    policy = read_json(path)
    ids, aliases = set(), {}
    for alias, tenant in policy["tenants"].items():
        if tenant["id"] in ids:
            raise LifecycleError("Policy contains duplicate tenant IDs/aliases")
        ids.add(tenant["id"])
        for name in [alias, tenant["id"], *tenant.get("aliases", [])]:
            if name in aliases and aliases[name] != tenant["id"]:
                raise LifecycleError("Policy contains ambiguous tenant aliases")
            aliases[name] = tenant["id"]
    return policy


def resolve_tenant(value, policy):
    """Aliases never change database identities, token AAD or bucket ownership."""
    if value in policy["tenants"]:
        return policy["tenants"][value]["id"]
    for entry in policy["tenants"].values():
        if value == entry["id"] or value in entry.get("aliases", []):
            return entry["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", value):
        raise LifecycleError(
            "New tenant slugs must use lowercase letters, numbers and hyphens"
        )
    return value


def bucket_name(project, tenant, user=None):
    """Preview existing NebiusUserStorage.bucket_name; never provisions a bucket."""

    def slug(value, limit, fallback):
        value = (
            unicodedata.normalize("NFKD", value)
            .encode("ascii", "ignore")
            .decode()
            .lower()
        )
        return (
            re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:limit].rstrip("-") or fallback
        )

    owner = user or ""
    suffix = hashlib.sha256(f"{project}\0{tenant}\0{owner}".encode()).hexdigest()[:16]
    parts = ["fs2", slug(tenant, 20 if owner else 42, "tenant")]
    if owner:
        parts.append(slug(owner, 21, "user"))
    return "-".join([*parts, suffix])


def validate_origin(base):
    parsed = urlsplit(base)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise LifecycleError("Use an API origin without credentials, query or fragment")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise LifecycleError(
            "HTTPS is required except for an explicitly local API connection"
        )


class AdminClient:
    def __init__(self, base, token_file, *, ca_file=None):
        validate_origin(base)
        self.base, self.cookie = base.rstrip("/"), None
        self.opener = build_opener(
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        )
        token = Path(token_file).read_text().strip()
        self.request(
            "POST",
            "/admin/api/v1/session",
            {},
            headers={"Authorization": f"Bearer {token}"},
        )
        if not self.cookie:
            raise LifecycleError("API did not issue an operator session")

    def request(self, method, path, payload=None, *, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise LifecycleError("API paths must be origin-relative")
        outgoing = {"Accept": "application/json", **(headers or {})}
        if self.cookie:
            outgoing["Cookie"] = self.cookie
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            outgoing["Content-Type"] = "application/json"
        try:
            with self.opener.open(
                Request(self.base + path, data=data, headers=outgoing, method=method),
                timeout=45,
            ) as response:
                for header in response.headers.get_all("Set-Cookie", []):
                    cookies = SimpleCookie()
                    cookies.load(header)
                    if "__Host-fs2_admin_session" in cookies:
                        self.cookie = (
                            "__Host-fs2_admin_session="
                            + cookies["__Host-fs2_admin_session"].value
                        )
                raw = response.read()
                value = json.loads(raw) if raw else {}
                return value.get("data", value)
        except HTTPError as exc:
            # Do not emit raw responses/headers: they may contain customer data.
            raise LifecycleError(
                f"API {method} {path.split('?')[0]} returned HTTP {exc.code}; inspect correlated server logs"
            ) from None
        except (URLError, TimeoutError):
            raise LifecycleError(
                f"API {method} transport failed; reconcile state before repeating a mutation"
            ) from None

    def close(self):
        if self.cookie:
            try:
                self.request("DELETE", "/admin/api/v1/session")
            finally:
                self.cookie = None


def users(client, tenant=None):
    query = {"limit": 1000}
    if tenant:
        query["tenant_id"] = tenant
    result = client.request("GET", "/admin/api/v1/users?" + urlencode(query))
    if result.get("truncated"):
        raise LifecycleError(
            "User list is truncated; use a narrower tenant, never act on a partial inventory"
        )
    return result["items"]


def select_user(client, tenant, principal):
    matches = [
        u
        for u in users(client, tenant)
        if u["tenant_id"] == tenant and u["principal_id"] == principal
    ]
    if len(matches) != 1:
        raise LifecycleError(
            "Expected exactly one matching tenant/user; no action performed"
        )
    return matches[0]


def wait_storage(client, user_id, desired, timeout):
    deadline = time.monotonic() + timeout
    while True:
        value = client.request("GET", f"/admin/api/v1/users/{user_id}/storage")
        if value["state"] == desired:
            # Explicit projection: access-key IDs and all secret fields are omitted.
            return {
                k: value.get(k)
                for k in (
                    "state",
                    "mode",
                    "bucket_name",
                    "endpoint",
                    "region",
                    "quota_bytes",
                    "examples",
                )
            }
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"Storage is still {value['state']}; reconciliation remains pending, not successful"
            )
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def create_user(
    client, tenant, principal, display_name, kind, mode, quota, *, timeout=180
):
    existing = [
        u
        for u in users(client, tenant)
        if u["tenant_id"] == tenant and u["principal_id"] == principal
    ]
    policy_path = f"/admin/api/v1/tenants/{quote(tenant, safe='')}/storage"
    current = client.request("GET", policy_path)
    if existing:
        if not existing[0]["enabled"]:
            raise LifecycleError(
                "User already exists but is disabled; explicit reactivation is a separate decision"
            )
        if current["mode"] != mode or current["quota_bytes"] != quota:
            raise LifecycleError(
                "Existing tenant storage differs; do not silently migrate or resize it"
            )
        return {
            "created": False,
            "user_id": existing[0]["id"],
            "storage": wait_storage(client, existing[0]["id"], "ready", timeout),
        }
    # Configure mode before creating the first user; backend rejects unsafe mode changes.
    if current["mode"] != mode or current["quota_bytes"] != quota:
        client.request("PUT", policy_path, {"mode": mode, "quota_bytes": quota})
    user = client.request(
        "POST",
        "/admin/api/v1/users",
        {
            "tenant_id": tenant,
            "principal_id": principal,
            "display_name": display_name,
            "kind": kind,
            "enabled": True,
        },
    )
    return {
        "created": True,
        "user_id": user["id"],
        "storage": wait_storage(client, user["id"], "ready", timeout),
    }


def retire_user(client, tenant, principal, *, apply=False, drained=False, timeout=180):
    user = select_user(client, tenant, principal)
    detail = client.request("GET", f"/admin/api/v1/users/{user['id']}")
    keys = detail["keys"]
    plan = {
        "tenant_id": tenant,
        "principal_id": principal,
        "user_id": user["id"],
        "actions": [
            "disable inference owner",
            "revoke inference keys",
            "wait for S3 key deactivation",
        ],
        "revoke_key_ids": [k["id"] for k in keys if not k.get("revoked_at")],
        "bucket_action": "retain all data and shared-user access",
        "history_action": "retain accounting and identity tombstone",
        "client_action": "separate backup and endpoint retirement required; no endpoints stopped here",
    }
    if not apply:
        return plan
    if not drained:
        raise LifecycleError(
            "Confirm the user's outstanding operations/clients are drained with --drained before revoking all keys"
        )
    usage = detail.get("user", {}).get("usage", {})
    if usage.get("pending", 0) or usage.get("running", 0):
        raise LifecycleError(
            "User has active operations in the API observation window; retirement was not started"
        )
    # Do not delete the identity: legacy discovery would resurrect it and S3 reconciliation would lose its owner.
    client.request("PATCH", f"/admin/api/v1/users/{user['id']}", {"enabled": False})
    for key_id in plan["revoke_key_ids"]:
        client.request("DELETE", f"/admin/api/v1/keys/{key_id}")
    if (detail.get("storage") or {}).get("bucket_name"):
        plan["storage"] = wait_storage(client, user["id"], "disabled", timeout)
    remaining = client.request("GET", f"/admin/api/v1/users/{user['id']}/keys")["items"]
    if any(not k.get("revoked_at") for k in remaining):
        raise LifecycleError(
            "Keys changed during retirement; reconcile again before declaring the user retired"
        )
    plan["status"] = "access_retired_data_retained"
    return plan


def issue_key(client, tenant, principal, spec, output):
    user = select_user(client, tenant, principal)
    if not user["enabled"]:
        raise LifecycleError("Cannot issue a key for a disabled owner")
    for field, value in (("tenant_id", tenant), ("principal_id", principal)):
        if field in spec and spec[field] != value:
            raise LifecycleError("Key specification names a different owner")
    if not spec.get("name") or not spec.get("models") or not spec.get("scopes"):
        raise LifecycleError(
            "Key specification must explicitly name the key, model grants and scopes"
        )
    existing = client.request("GET", f"/admin/api/v1/users/{user['id']}/keys")["items"]
    if any(k.get("name") == spec["name"] and not k.get("revoked_at") for k in existing):
        raise LifecycleError(
            "That key name already exists; do not mint a duplicate or rotate access implicitly"
        )
    # Reserve the private output before issuing a one-time secret.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(output, flags, 0o600), "w") as handle:
        result = client.request(
            "POST",
            f"/admin/api/v1/users/{user['id']}/keys",
            {**spec, "tenant_id": tenant, "principal_id": principal},
        )
        json.dump(result, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "key_id": result["key"]["id"],
        "handover_file": str(output),
        "secret_printed": False,
    }


def consolidation_plan(snapshot, policy):
    """A review plan, never an automatic prune based on an allowlist."""
    db = snapshot["database"]
    retained = {v["id"] for v in policy["tenants"].values()}
    preserve = {
        alias: {"id": v["id"], "users": v.get("users", []), "purpose": v["purpose"]}
        for alias, v in policy["tenants"].items()
    }
    owned = sorted({u["tenant_id"] for u in db["users"] + db["storage_identities"]})
    return {
        "snapshot_at": snapshot["captured_at"],
        "desired_tenants": preserve,
        "new_tenants": sorted(retained - set(owned)),
        "legacy_tenants_to_classify_or_migrate": sorted(set(owned) - retained),
        "keep_customer_bindings": {
            b["tenant_id"]: b["bucket_name"]
            for b in db["storage_buckets"]
            if b["tenant_id"] in {"rene", "kopra"}
        },
        "retirement_requires": [
            "current owner/task confirmation",
            "drained work",
            "client-local backup",
            "API and S3 access retirement",
            "data retention decision",
        ],
        "delete_buckets": [],
        "stop_endpoints": [],
        "apply_supported": False,
    }


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=os.environ.get("FS2_ADMIN_URL"))
    p.add_argument("--admin-token-file", default=os.environ.get("FS2_ADMIN_TOKEN_FILE"))
    p.add_argument("--ca-file")
    p.add_argument("--policy", default=os.environ.get("FS2_TENANT_POLICY"))
    sub = p.add_subparsers(dest="command", required=True)
    name = sub.add_parser("bucket-name")
    name.add_argument("--project", required=True)
    name.add_argument("--tenant", required=True)
    name.add_argument("--user")
    inventory = sub.add_parser("inventory")
    inventory.add_argument("--tenant")
    inventory.add_argument("--include-disabled", action="store_true")
    plan = sub.add_parser("consolidation-plan")
    plan.add_argument("--snapshot", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("--display-name")
    create.add_argument("--kind", choices=["human", "service"], default="human")
    create.add_argument("--mode", choices=["tenant", "user"])
    create.add_argument("--quota-bytes", type=int)
    retire = sub.add_parser(
        "retire-user",
        help="disable/revoke access; retain bucket, history and owner tombstone",
    )
    retire.add_argument("--drained", action="store_true")
    key = sub.add_parser("issue-key")
    key.add_argument("--spec", required=True)
    key.add_argument("--output", required=True)
    creds = sub.add_parser("storage-credentials")
    creds.add_argument("--output", required=True)
    for action in (create, retire, key, creds):
        action.add_argument("--tenant", required=True)
        action.add_argument("--user", required=True)
        action.add_argument("--apply", action="store_true")
        action.add_argument("--wait-seconds", type=float, default=180)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    policy = load_policy(args.policy)
    tenant = (
        resolve_tenant(args.tenant, policy) if getattr(args, "tenant", None) else None
    )
    if args.command == "create-user":
        desired = next((t for t in policy["tenants"].values() if t["id"] == tenant), {})
        args.mode = args.mode or desired.get(
            "mode", policy.get("default_storage_mode", "tenant")
        )
        args.quota_bytes = (
            args.quota_bytes
            if args.quota_bytes is not None
            else desired.get(
                "quota_bytes", policy.get("default_quota_bytes", 5_000_000_000)
            )
        )
        if (
            args.mode not in {"tenant", "user"}
            or args.quota_bytes <= 0
            or args.wait_seconds < 0
        ):
            raise LifecycleError("Storage mode/quota/wait duration is invalid")
    if args.command == "bucket-name":
        return {
            "bucket_name": bucket_name(args.project, tenant, args.user),
            "source": "name preview, not a provisioned resource",
        }
    if args.command == "consolidation-plan":
        return consolidation_plan(read_json(args.snapshot), policy)
    if args.command == "create-user" and not args.apply:
        return {
            "action": "create_or_reuse_user",
            "tenant_id": tenant,
            "principal_id": args.user,
            "storage_mode": args.mode,
            "quota_bytes": args.quota_bytes,
            "bucket_owner": args.user if args.mode == "user" else "shared tenant",
            "existing_keys": "preserve",
            "apply": False,
        }
    if args.command in {"issue-key", "storage-credentials"} and not args.apply:
        return {
            "action": args.command,
            "tenant_id": tenant,
            "principal_id": args.user,
            "output": args.output,
            "apply": False,
        }
    if not args.base_url or not args.admin_token_file:
        raise LifecycleError(
            "Set --base-url and --admin-token-file (or FS2_ADMIN_URL/FS2_ADMIN_TOKEN_FILE)"
        )
    client = AdminClient(args.base_url, args.admin_token_file, ca_file=args.ca_file)
    try:
        if args.command == "inventory":
            rows = users(client, tenant)
            return [
                {
                    k: u.get(k)
                    for k in (
                        "id",
                        "tenant_id",
                        "principal_id",
                        "display_name",
                        "enabled",
                        "source",
                        "key_count",
                        "active_key_count",
                    )
                }
                for u in rows
                if args.include_disabled or u["enabled"]
            ]
        if args.command == "create-user":
            if args.quota_bytes <= 0 or args.wait_seconds < 0:
                raise LifecycleError(
                    "Quota must be positive; wait duration must be nonnegative"
                )
            return create_user(
                client,
                tenant,
                args.user,
                args.display_name or args.user,
                args.kind,
                args.mode,
                args.quota_bytes,
                timeout=args.wait_seconds,
            )
        if args.command == "retire-user":
            return retire_user(
                client,
                tenant,
                args.user,
                apply=args.apply,
                drained=args.drained,
                timeout=args.wait_seconds,
            )
        if args.command == "issue-key":
            return issue_key(
                client, tenant, args.user, read_json(args.spec), args.output
            )
        if args.command == "storage-credentials":
            user = select_user(client, tenant, args.user)
            if Path(args.output).exists():
                raise LifecycleError(
                    "Credential output already exists; use a new private handover file"
                )
            result = client.request(
                "POST", f"/admin/api/v1/users/{user['id']}/storage/credentials", {}
            )
            private_output(args.output, result)
            return {"handover_file": args.output, "secret_printed": False}
    finally:
        client.close()


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2))
    except (LifecycleError, OSError, ValueError) as error:
        print(f"Lifecycle command did not complete: {error}", file=sys.stderr)
        sys.exit(1)
