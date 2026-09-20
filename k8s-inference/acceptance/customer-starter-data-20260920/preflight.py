#!/usr/bin/env python3
"""Inventory authoritative workspace records; optionally check create-only S3.

--canary creates/adopts one task-owned active user, never re-enables a disabled
identity, and retains one tiny test object. No customer object body is read,
no project-wide S3 key is used, and no secret is printed or persisted.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

TENANT = "fs2-starter-data-acceptance-20260920"
PRINCIPAL = "seed-canary"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "fs2-system",
        "get",
        "secret",
        "fs2-serve-admin",
        "-o",
        "json",
    ]
    secret = json.loads(subprocess.check_output(command))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    report = {"at": datetime.now(UTC).isoformat(), "inventory": []}
    with httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        timeout=30,
        trust_env=False,
    ) as api:
        response = api.post(
            "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
        )
        response.raise_for_status()

        def request(method, path, payload=None):
            response = api.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()["data"]

        users = request("GET", "/admin/api/v1/users?limit=1000")["items"]
        for user in users:
            if not user["enabled"]:
                continue
            state = request("GET", f"/admin/api/v1/users/{user['id']}/storage")
            report["inventory"].append(
                {
                    "user_id": user["id"],
                    "tenant_id": user["tenant_id"],
                    "principal_id": user["principal_id"],
                    "state": state["state"],
                    "mode": state["mode"],
                    "bucket_name": state.get("bucket_name"),
                    "quota_bytes": state["quota_bytes"],
                }
            )
        print(json.dumps(report), flush=True)
        if not args.canary:
            return
        matches = [
            u
            for u in users
            if u["tenant_id"] == TENANT and u["principal_id"] == PRINCIPAL
        ]
        if matches:
            user = matches[0]
            if not user["enabled"]:
                raise RuntimeError("task_fixture_disabled_not_reactivated")
        else:
            user = request(
                "POST",
                "/admin/api/v1/users",
                {
                    "tenant_id": TENANT,
                    "principal_id": PRINCIPAL,
                    "display_name": "Starter-data create-only acceptance",
                    "kind": "service",
                    "app_ids": [],
                },
            )
        path = f"/admin/api/v1/users/{user['id']}/storage"
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            state = request("GET", path)
            if state["state"] == "ready":
                break
            if state["state"] == "disabled":
                raise RuntimeError("task_fixture_storage_disabled")
            time.sleep(5)
        else:
            raise RuntimeError("task_fixture_provisioning_timeout")
        credentials = request("POST", path + "/credentials")
        client = boto3.client(
            "s3",
            endpoint_url=credentials["endpoint"],
            region_name=credentials["region"],
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            config=Config(
                connect_timeout=10, read_timeout=30, retries={"max_attempts": 1}
            ),
        )
        bucket = credentials["bucket_name"]
        key = f"examples-preflight/{uuid4()}/create-only.txt"
        original = b"task-owned create-only preflight v1\n"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=original,
            IfNoneMatch="*",
            ContentType="text/plain",
        )
        status = "unexpected_overwrite"
        try:
            client.put_object(
                Bucket=bucket, Key=key, Body=b"must not replace v1\n", IfNoneMatch="*"
            )
        except ClientError as exc:
            status = str(exc.response["ResponseMetadata"]["HTTPStatusCode"])
        response = client.get_object(Bucket=bucket, Key=key)
        try:
            retained = response["Body"].read(1024)
        finally:
            response["Body"].close()
        result = {
            "at": datetime.now(UTC).isoformat(),
            "user_id": user["id"],
            "bucket": bucket,
            "key": key,
            "second_put_status": status,
            "original_preserved": retained == original,
            "sha256": hashlib.sha256(retained).hexdigest(),
            "bytes": len(retained),
            "scope": "task-owned probe only; retained; no customer object modified",
        }
        print(json.dumps(result), flush=True)
        if status != "412" or retained != original:
            raise RuntimeError("provider_create_only_contract_not_proven")


if __name__ == "__main__":
    main()
