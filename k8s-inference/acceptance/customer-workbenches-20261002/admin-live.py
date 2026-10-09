"""Operate the customer lifecycle API without exposing administrator credentials.

Inventory is read-only. Adopt records metadata only and never changes cloud
resources. Upgrade must name an existing binding and qualified release explicitly.
"""
import argparse
import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "cloud", "browser-auth", "adopt", "profile", "upgrade", "detail"])
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    parser.add_argument("--tenant")
    parser.add_argument("--principal")
    parser.add_argument("--endpoint")
    parser.add_argument("--project", default="project-e00rene")
    parser.add_argument("--name")
    parser.add_argument("--purpose", default="customer")
    parser.add_argument("--protected", action="store_true")
    parser.add_argument("--workbench")
    parser.add_argument("--release")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--confirm-interruption", action="store_true")
    args = parser.parse_args()
    raw = subprocess.check_output(["kubectl", "--context", args.context, "-n", "fs2-system", "get", "secret",
                                   "fs2-serve-admin", "-o", "json"])
    key = base64.b64decode(json.loads(raw)["data"]["token"]).decode().strip()
    with httpx.Client(base_url=args.origin, timeout=90, trust_env=False,
                      headers={"Origin": args.origin, "X-Requested-With": "XMLHttpRequest"}) as client:
        session = client.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + key})
        session.raise_for_status()
        if args.action == "inventory":
            client.post("/admin/api/v1/workbench-inventory/refresh").raise_for_status()
            response = client.get("/admin/api/v1/customers")
            response.raise_for_status()
            value = response.json()["data"]
            print(json.dumps({"customers": [{"tenant": item["tenant_id"],
                "principals": [user["principal_id"] for user in item["users"]],
                "buckets": [bucket["bucket_name"] for bucket in item["buckets"]],
                "workbenches": item["workbenches"], "requests": item["requests"]} for item in value["items"]],
                "inventory_error": value["inventory_error"]}, default=str))
        elif args.action == "browser-auth":
            if not args.private_output:
                parser.error("browser-auth requires a private output path")
            cookies = [{"name": c.name, "value": c.value, "domain": urlsplit(args.origin).hostname,
                        "path": c.path, "expires": c.expires or -1, "httpOnly": True,
                        "secure": True, "sameSite": "Strict"} for c in client.cookies.jar]
            fd = os.open(args.private_output, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump({"cookies": cookies, "origins": []}, output)
            print("Private browser authentication state saved; credential not logged.")
        elif args.action == "cloud":
            response = client.get("/admin/api/v1/workbench-inventory")
            response.raise_for_status()
            items = response.json()["data"]["items"]
            print(json.dumps([item for item in items if item["kind"] == "endpoint" and item.get("state") == "RUNNING"]))
        elif args.action == "adopt":
            if not all([args.tenant, args.principal, args.endpoint, args.name]):
                parser.error("adopt requires tenant, principal, endpoint and name")
            response = client.post("/admin/api/v1/workbenches", json={"tenant_id": args.tenant,
                "principal_ids": [args.principal], "name": args.name, "endpoint_id": args.endpoint,
                "project_id": args.project, "protected": args.protected,
                "protection_reason": "Owner hold: do not modify" if args.protected else ""})
            response.raise_for_status()
            print(json.dumps(response.json()["data"], default=str))
        elif args.action == "profile":
            response = client.put(f"/admin/api/v1/customers/{args.tenant}/profile", json={
                "display_name": args.name, "purpose": args.purpose, "archived": False})
            response.raise_for_status()
            print(json.dumps(response.json()["data"]))
        elif args.action == "detail":
            response = client.get(f"/admin/api/v1/customers/{args.tenant}")
            response.raise_for_status()
            value = response.json()["data"]["customer"]
            print(json.dumps({"tenant": value["tenant_id"], "workbenches": value["workbenches"]}))
        elif args.action == "upgrade":
            if not all([args.workbench, args.release, args.revision, args.idempotency_key, args.confirm_interruption]):
                parser.error("upgrade requires binding, release, revision, idempotency key and interruption confirmation")
            response = client.post(f"/admin/api/v1/workbenches/{args.workbench}/operations", json={
                "kind": "upgrade", "target_release": args.release, "expected_revision": args.revision,
                "idempotency_key": args.idempotency_key, "confirm_interruption": True})
            response.raise_for_status()
            print(json.dumps(response.json()["data"]))


if __name__ == "__main__":
    main()
