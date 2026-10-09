#!/usr/bin/env python3
"""Verify ready users while new bucket provisioning is quota-blocked.

Creates/revokes two short-lived catalog-only platform API keys for Rene. Only
one random task-owned S3 object is written and then removed. Does not create
cloud identities, disable customers, or imply full private-mode acceptance.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from boto3.s3.transfer import TransferConfig
from verify import require, s3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-system"]
    secret = json.loads(subprocess.check_output(command + ["get", "secret", "fs2-serve-admin", "-o", "json"]))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    key_ids = []
    obj = None
    checks = []
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False) as admin:
        def call(method, path, payload=None, status=200):
            r = admin.request(method, path, json=payload)
            require(r.status_code == status, f"{method}_{path}_{r.status_code}")
            return r.json()["data"]

        try:
            r = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
            require(r.status_code == 200, "login")
            items = call("GET", "/admin/api/v1/users?tenant_id=rene")["items"]
            user = next(u for u in items if u["principal_id"] == "rene")
            path = f"/admin/api/v1/users/{user['id']}"
            credentials = call("POST", path + "/storage/credentials")
            detail = call("GET", path)
            require("secret_access_key" not in json.dumps(detail), "ordinary_detail_no_secret")
            client = s3(credentials)
            bucket, object_key = credentials["bucket_name"], "acceptance/customer-storage/" + str(uuid4())
            obj = (client, bucket, object_key)
            payload = os.urandom(17 * 1024 * 1024)
            client.upload_fileobj(io.BytesIO(payload), bucket, object_key, Config=TransferConfig(
                multipart_threshold=5 * 1024 * 1024, multipart_chunksize=5 * 1024 * 1024, max_concurrency=4))
            result = client.get_object(Bucket=bucket, Key=object_key)["Body"].read()
            require(hashlib.sha256(result).digest() == hashlib.sha256(payload).digest(), "multipart_checksum")
            checks.append("17_MiB_multipart_upload_download_checksum")
            for _ in range(2):
                disclosure = call("POST", path + "/keys", {
                    "name": "storage-acceptance-" + str(uuid4()),
                    "tenant_id": "rene", "principal_id": "rene", "models": ["*"], "scopes": ["catalog.read"],
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                }, 201)
                key_ids.append(disclosure["key"]["id"])
                with httpx.Client(base_url=args.origin, headers={"authorization": "Bearer " + disclosure["secret"]},
                                  timeout=60, trust_env=False) as public:
                    info = public.get("/v1/storage")
                    require(info.status_code == 200 and info.json()["bucket_name"] == bucket, "public_own_bucket")
                    require("secret_access_key" not in info.json(), "metadata_no_secret")
                    r = public.post("/v1/storage/credentials")
                    require(r.status_code == 200 and r.json() == credentials, "public_same_credentials")
                    require(r.headers.get("cache-control") == "no-store", "no_store")
                    require(public.get(path + "/storage").status_code == 401, "inference_key_not_operator")
            checks.extend(["two_platform_keys_same_user_same_s3_credentials", "metadata_no_secret",
                           "explicit_secret_response_no_store", "inference_key_cannot_access_admin_storage"])
        finally:
            if obj:
                obj[0].delete_object(Bucket=obj[1], Key=obj[2])
            for key_id in key_ids:
                call("DELETE", "/admin/api/v1/keys/" + key_id)
            admin.delete("/admin/api/v1/session")
    print(json.dumps({"scope": "existing_ready_user_only", "outcome": "passed", "checks": checks,
                      "test_object_deleted": True, "temporary_api_keys_revoked": len(key_ids),
                      "full_provisioning": "blocked_cloud_bucket_policy_quota"}), flush=True)


if __name__ == "__main__":
    main()
