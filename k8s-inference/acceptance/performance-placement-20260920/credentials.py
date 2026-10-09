"""Create one expiring, task-owned benchmark identity without a customer bucket."""

import argparse
import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inventory import admin_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    path = args.directory / "inference-token.private"
    if path.exists():
        raise RuntimeError("benchmark credential already exists; reuse it")
    tenant, principal = "platform-benchmarks", "performance-20260920"

    def checked(response):
        response.raise_for_status()
        return response.json()["data"]

    with closing(admin_client(args.kubeconfig, args.context, args.origin)) as admin:
        checked(admin.put(f"/admin/api/v1/tenants/{tenant}/storage", json={"mode": "disabled", "quota_bytes": 5000000000}))
        users = checked(admin.get("/admin/api/v1/users", params={"tenant_id": tenant}))["items"]
        user = next((user for user in users if user["principal_id"] == principal), None)
        if user is None:
            user = checked(admin.post("/admin/api/v1/users", json={
                "tenant_id": tenant, "principal_id": principal, "display_name": "Platform performance campaign",
                "kind": "service",
            }))
        key = checked(admin.post(f"/admin/api/v1/users/{user['id']}/keys", json={
            "tenant_id": tenant, "principal_id": principal, "name": "performance-20260920", "models": ["*"],
            "scopes": ["catalog.read", "inference.invoke", "operations.read", "operations.result", "operations.cancel",
                       "artifacts.write", "use.nonclinical", "use.noncommercial"],
            "max_concurrency": 4, "request_budget": 2000,
            "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        }))
        with path.open("x") as file:
            file.write(key.pop("secret"))
        (args.directory / "identity.json").write_text(json.dumps({"user_id": user["id"], "key": key}, indent=2))
        print(json.dumps({"tenant": tenant, "principal": principal, "credential_stored": True, "concurrency": 4}))


if __name__ == "__main__":
    main()
