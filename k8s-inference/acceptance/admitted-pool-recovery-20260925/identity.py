#!/usr/bin/env python3
"""Create/retire only this task's disposable ordinary GROMACS identity.

Uses the established operator session route. The approved source key's policy
is copied without widening scopes, concurrency, budgets, or rate limits.
Existing keys and owners are never changed.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import httpx

from run_acceptance import GateError, Kube, TENANT, TERMINAL, ordinary_policy, read, require, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "retire"])
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--tenant", default=TENANT)
    parser.add_argument("--source-key-file", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    require(re.fullmatch(TENANT + r"(?:-[a-z0-9]+)?", args.tenant), "wrong_task_tenant")
    os.umask(0o077)
    kube = Kube(args.kubeconfig, args.context)
    secret = kube.json("get", "secret", "fs2-serve-admin", "-n", "fs2-system", "-o", "json")
    token = base64.b64decode(secret["data"]["token"]).decode().strip()

    def checked(response):
        require(response.is_success, "identity_admin_request_failed")
        return response.json()

    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, trust_env=False, timeout=60) as client:
        checked(client.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token}))
        try:
            if args.action == "create":
                require(args.source_key_file is not None, "source_key_policy_required")
                source = read(args.source_key_file)["key"]
                candidate = {**source, "tenant_id": args.tenant, "principal_id": "pool-recovery-test", "models": ["gromacs"]}
                policy = ordinary_policy(candidate, args.tenant)
                require("gromacs" in source["models"], "source_key_does_not_grant_gromacs")
                require(not checked(client.get("/admin/api/v1/users", params={"tenant_id": args.tenant}))["data"]["items"], "tenant_already_exists")
                args.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
                owner = checked(client.post("/admin/api/v1/users", json={
                    "tenant_id": args.tenant, "principal_id": policy["principal_id"], "kind": "service",
                    "display_name": "Admitted pool recovery acceptance (temporary)", "team": TENANT,
                    "enabled": True, "app_ids": None}))["data"]
                save(args.directory / "owner.json", owner)
                payload = {**policy, "name": TENANT, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()}
                for field in ("request_budget", "gpu_seconds_budget", "rate_limit_requests", "rate_window_seconds"):
                    payload[field] = source.get(field)
                issued = checked(client.post("/admin/api/v1/keys", json=payload))["data"]
                save(args.directory / "key-private.json", {"key": issued["key"], "secret": issued["secret"], "disposable": True})
                receipt = {"action": "created", "tenant_id": args.tenant, "owner_id": owner["id"], "key_id": issued["key"]["id"],
                           "source_key_id": source["id"], "policy": {k: payload[k] for k in payload if k not in {"name", "expires_at"}},
                           "existing_customer_policy_changed": False, "at": datetime.now(timezone.utc).isoformat()}
                save(args.directory / "created.json", receipt)
            else:
                owner, key = read(args.directory / "owner.json"), read(args.directory / "key-private.json")
                require(owner["tenant_id"] == key["key"]["tenant_id"] == args.tenant and owner["team"] == TENANT
                        and key["key"]["name"] == TENANT and key.get("disposable") is True, "retirement_not_task_owned")
                require(not any(kube.owned(args.tenant)), "cannot_retire_active_task_resources")
                history = checked(client.get("/v1/operations", params={"limit": 200},
                                  headers={"Authorization": "Bearer " + key["secret"]}))
                require(not history["next_cursor"] and all(row["status"] in TERMINAL for row in history["data"]), "cannot_retire_active_task_operations")
                revoked = checked(client.delete("/admin/api/v1/keys/" + key["key"]["id"]))["data"]
                require(bool(revoked["revoked_at"]), "key_revocation_not_persisted")
                disabled = checked(client.patch("/admin/api/v1/users/" + owner["id"], json={"enabled": False}))["data"]
                require(disabled["enabled"] is False, "owner_disable_not_persisted")
                receipt = {"action": "retired", "tenant_id": args.tenant, "owner_id": owner["id"], "key_id": key["key"]["id"],
                           "key_revoked": True, "owner_disabled": True, "artifacts_deleted": False,
                           "at": datetime.now(timezone.utc).isoformat()}
                save(args.directory / "retired.json", receipt)
        finally:
            client.delete("/admin/api/v1/session")
    print(json.dumps({key: receipt[key] for key in ("action", "tenant_id", "owner_id", "key_id")}))


if __name__ == "__main__":
    try:
        main()
    except GateError as error:
        print(json.dumps({"state": "failed", "error": str(error)}))
        raise SystemExit(1) from None
