"""Apply the owner's approved Lynx 100 GB bucket allowance in place.

Preview is the default. Only the existing operator storage-policy API writes
state; the normal reconciler updates the provider. No customer inference key or
S3 secret is loaded, no object is written, and no cloud quota is increased.
Run with the control-plane Python environment (httpx2).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx2

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
ORIGIN = "https://89.169.99.188"
BUCKET = "fs2-lynx-c327dcc386444425"
BUCKET_ID = "storagebucket-e007042062916934702682"
TARGET_BYTES = 100_000_000_000
USER_ID = uuid5(NAMESPACE_URL, "fs2:inference-owner:lynx:lynx")


def provider_bucket() -> dict:
    return json.loads(subprocess.check_output([  # noqa: S603 - exact operator-owned target, no shell
        "/usr/local/bin/nebius", "--profile", "sandbox2", "--no-browser", "--format", "json",
        "storage", "bucket", "get-by-name", "--parent-id", "project-e00rene",
        "--name", BUCKET,
    ], timeout=30))


def redacted_storage(value: dict) -> dict:
    result = dict(value)
    key_id = result.pop("access_key_id", None)
    result["access_key_id_sha256"] = hashlib.sha256((key_id or "").encode()).hexdigest()
    return result


def main(args: argparse.Namespace) -> None:
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)

    def save(name: str, value: dict) -> None:
        # Evidence files contain provider metadata, never tokens or S3 secrets.
        path = args.output / name
        with path.open("x") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")

    secret = json.loads(subprocess.check_output([  # noqa: S603 - exact operator credential source
        "/snap/bin/kubectl", "--context", CONTEXT, "--request-timeout=20s", "-n", "fs2-system",
        "get", "secret", "fs2-serve-admin", "-o", "json",
    ], timeout=30))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx2.Client(base_url=ORIGIN, timeout=30, trust_env=False,
                       headers={"Origin": ORIGIN, "X-Requested-With": "XMLHttpRequest"}) as admin:
        response = admin.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        try:
            def get_data(path: str) -> dict:
                result = admin.get(path)
                result.raise_for_status()
                return result.json()["data"]

            storage_path = f"/admin/api/v1/users/{USER_ID}/storage"
            policy_path = "/admin/api/v1/tenants/lynx/storage"
            before = provider_bucket()
            policy = get_data(policy_path)
            binding = get_data(storage_path)
            if (before["metadata"]["id"] != BUCKET_ID
                    or before["metadata"]["parent_id"] != "project-e00rene"
                    or policy["mode"] != "tenant"
                    or policy["quota_bytes"] not in (5_000_000_000, TARGET_BYTES)
                    or binding["mode"] != "tenant" or binding["state"] != "ready"
                    or binding["bucket_name"] != BUCKET
                    or int(before["spec"]["max_size_bytes"]) not in (5_000_000_000, TARGET_BYTES)):
                raise RuntimeError("Existing Lynx storage differs from the approved target; no mutation")
            save("before.json", {"provider": before, "policy": policy,
                                 "storage": redacted_storage(binding)})
            plan = {"tenant": "lynx", "bucket_id": BUCKET_ID, "bucket_name": BUCKET,
                    "old_bytes": int(before["spec"]["max_size_bytes"]),
                    "target_bytes": TARGET_BYTES, "apply": args.apply,
                    "customer_inference_key_used": False, "customer_s3_secret_used": False,
                    "cloud_quota_change": False, "bucket_replacement": False}
            save("plan.json", plan)
            if not args.apply:
                print(json.dumps(plan))
                return

            if policy["quota_bytes"] != TARGET_BYTES:
                response = admin.put(policy_path, json={"mode": "tenant", "quota_bytes": TARGET_BYTES})
                response.raise_for_status()
                save("accepted-policy.json", response.json()["data"])
            deadline = time.monotonic() + 300
            while True:
                current = get_data(storage_path)
                after = provider_bucket()
                if current["quota_bytes"] == TARGET_BYTES and int(after["spec"]["max_size_bytes"]) == TARGET_BYTES:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Policy accepted but provider reconciliation not verified yet")
                time.sleep(5)

            if after["metadata"]["id"] != before["metadata"]["id"]:
                raise RuntimeError("Unexpected bucket identity change")
            allowed = {"max_size_bytes"}
            changes = {key for key in before["spec"].keys() | after["spec"].keys()
                       if before["spec"].get(key) != after["spec"].get(key)}
            if changes - allowed:
                raise RuntimeError("Unexpected provider spec change")
            for field in ("mode", "state", "bucket_name", "endpoint", "region", "access_key_id"):
                if current[field] != binding[field]:
                    raise RuntimeError(f"Unexpected storage binding change: {field}")
            save("after.json", {"provider": after, "policy": get_data(policy_path),
                                "storage": redacted_storage(current)})
            receipt = {**plan, "verified_at": datetime.now(UTC).isoformat(),
                       "verified": True, "binding_preserved": True,
                       "provider_changed_fields": sorted(changes), "objects_deleted": False,
                       "credentials_rotated": False}
            save("verification.json", receipt)
            print(json.dumps(receipt))
        finally:
            admin.delete("/admin/api/v1/session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
