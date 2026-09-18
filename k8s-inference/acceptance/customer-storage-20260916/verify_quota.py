#!/usr/bin/env python3
"""Verify Nebius bucket byte-limit rejection on a disposable 1 MiB fixture.

The provider documents that rapid writes can temporarily overshoot max_size_bytes,
so the test uses paced sequential writes and accepts only an explicit
BucketMaxSizeExceeded response. It never changes a customer quota and always
deletes accepted fixture objects and disables the fixture user.
"""

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import httpx
from botocore.exceptions import ClientError

FIXTURE_TENANT = "fs2-storage-quota-acceptance-20260918"
FIXTURE_PRINCIPAL = "overflow"
FIXTURE_QUOTA = 1024 * 1024
WRITE_BYTES = 2 * 1024 * 1024


def require(value, code):
    if not value:
        raise AssertionError(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-system"]
    raw = json.loads(subprocess.check_output(kubectl + ["get", "secret", "fs2-serve-admin", "-o", "json"]))
    admin_token = base64.b64decode(raw["data"]["token"]).decode().strip()
    accepted: list[tuple[object, str, str]] = []
    user = None
    evidence = {
        "started_at": datetime.now(UTC).isoformat(),
        "tenant": FIXTURE_TENANT,
        "configured_quota_bytes": FIXTURE_QUOTA,
        "write_size_bytes": WRITE_BYTES,
    }

    with httpx.Client(
        base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False
    ) as admin:
        def request(method, path, payload=None, expected=200):
            response = admin.request(method, path, json=payload)
            require(response.status_code == expected, f"admin_{method}_{path}_{response.status_code}")
            return response.json()["data"]

        def wait_for(user_id, wanted, seconds=480):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                value = request("GET", f"/admin/api/v1/users/{user_id}/storage")
                if value["state"] == wanted:
                    return value
                time.sleep(5)
            raise AssertionError(f"storage_{wanted}_timeout")

        try:
            login = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + admin_token})
            require(login.status_code == 200, "admin_login")
            request(
                "PUT",
                f"/admin/api/v1/tenants/{FIXTURE_TENANT}/storage",
                {"mode": "user", "quota_bytes": FIXTURE_QUOTA},
            )
            listing = request("GET", f"/admin/api/v1/users?tenant_id={FIXTURE_TENANT}&limit=100")
            found = [item for item in listing["items"] if item["principal_id"] == FIXTURE_PRINCIPAL]
            if found:
                user = request("PATCH", f"/admin/api/v1/users/{found[0]['id']}", {"enabled": True})
            else:
                user = request(
                    "POST",
                    "/admin/api/v1/users",
                    {
                        "tenant_id": FIXTURE_TENANT,
                        "principal_id": FIXTURE_PRINCIPAL,
                        "display_name": "Storage quota acceptance overflow",
                        "kind": "service",
                        "app_ids": [],
                    },
                    201,
                )
            state = wait_for(user["id"], "ready")
            require(state["quota_bytes"] == FIXTURE_QUOTA, "fixture_quota_mismatch")
            response = admin.post(f"/admin/api/v1/users/{user['id']}/storage/credentials")
            require(response.status_code == 200, "credential_disclosure")
            value = response.json()["data"]
            client = boto3.client(
                "s3",
                endpoint_url=value["endpoint"],
                region_name=value["region"],
                aws_access_key_id=value["access_key_id"],
                aws_secret_access_key=value["secret_access_key"],
            )
            rejection = None
            payload = os.urandom(WRITE_BYTES)
            for _ in range(6):
                key = "acceptance/customer-storage/quota-" + str(uuid4())
                try:
                    client.put_object(Bucket=value["bucket_name"], Key=key, Body=payload)
                    accepted.append((client, value["bucket_name"], key))
                except ClientError as error:
                    rejection = {
                        "status": error.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
                        "code": error.response.get("Error", {}).get("Code"),
                    }
                    break
                time.sleep(20)
            require(rejection is not None, "provider_did_not_reject_paced_writes")
            require(rejection["status"] == 400, "unexpected_quota_http_status")
            require(rejection["code"] == "BucketMaxSizeExceeded", "unexpected_quota_error")
            evidence.update(
                {
                    "accepted_writes_before_rejection": len(accepted),
                    "provider_rejection": rejection,
                    "outcome": "passed",
                }
            )
        finally:
            for client, bucket, key in accepted:
                client.delete_object(Bucket=bucket, Key=key)
            if user is not None:
                request("PATCH", f"/admin/api/v1/users/{user['id']}", {"enabled": False})
                wait_for(user["id"], "disabled", 180)
            admin.delete("/admin/api/v1/session")
            evidence["cleanup"] = "accepted objects deleted; fixture user disabled"
            evidence["completed_at"] = datetime.now(UTC).isoformat()
            print(json.dumps(evidence, sort_keys=True), flush=True)
    return 0 if evidence.get("outcome") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
