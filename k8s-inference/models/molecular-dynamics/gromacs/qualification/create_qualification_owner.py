"""Create one isolated MD test owner using ordinary admin APIs and default storage.

Never modifies another tenant, changes storage limits, or deletes customer data.
The owner and its short-lived model-scoped key must be retired after evidence is
retained. Credentials are written only into a new owner-private output directory.
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import httpx

from release_access import private, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--source-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"md-qualification-[0-9]{8}", args.tenant):
        raise ValueError("Use an explicit task-owned qualification tenant")
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = json.loads(args.source_key_file.read_text())["key"]
    secret = json.loads(subprocess.check_output([
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json",
    ]))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    origin = "https://89.169.99.188"
    with httpx.Client(base_url=origin, timeout=90, trust_env=False,
                      headers={"origin": origin}) as client:
        result(client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}))
        existing = result(client.get("/admin/api/v1/users", params={"tenant_id": args.tenant}))["data"]["items"]
        if existing:
            raise ValueError("Qualification tenant already has an owner; inspect before reuse")
        owner = result(client.post("/admin/api/v1/users", json={
            "tenant_id": args.tenant, "principal_id": "md-test", "kind": "service",
            "display_name": "MD onboarding qualification (temporary)",
            "team": "native-md-20260923", "enabled": True, "app_ids": None,
        }))["data"]
        private(args.output / "owner.json", owner)
        issued = result(client.post("/admin/api/v1/keys", json={
            "tenant_id": args.tenant, "principal_id": "md-test",
            "name": "native-md-qualification-20260923",
            "models": ["lammps", "namd"], "scopes": source["scopes"],
            "max_concurrency": min(3, source["max_concurrency"]),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
        }))["data"]
        private(args.output / "key-private.json", {
            "secret": issued["secret"], "key": issued["key"], "disposable": True,
        })
        policy = result(client.get(f"/admin/api/v1/tenants/{args.tenant}/storage"))["data"]
        private(args.output / "storage-policy.json", policy)
        client.delete("/admin/api/v1/session")
    receipt = {"tenant_id": args.tenant, "principal_id": "md-test", "key_id": issued["key"]["id"],
               "storage_limits_changed": False, "customer_data_deleted": False,
               "created_at": datetime.now(timezone.utc).isoformat()}
    private(args.output / "receipt.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
