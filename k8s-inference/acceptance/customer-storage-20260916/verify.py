#!/usr/bin/env python3
"""Live customer-storage acceptance; no GPU jobs and no printed credentials.

Creates two explicitly named fixture users, then disables them and revokes their
temporary inference keys. Empty fixture buckets are retained for repeatability.
All uploaded objects are random, task-owned fixtures and are deleted in finally.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3
import httpx
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

FIXTURE_TENANT = "fs2-storage-acceptance-20260916"


def require(value, code):
    if not value:
        raise AssertionError(code)


def denied(call):
    try:
        call()
    except ClientError as error:
        require(error.response["ResponseMetadata"]["HTTPStatusCode"] == 403, "expected_s3_403")
        return
    raise AssertionError("unexpected_s3_access")


def s3(credentials):
    return boto3.client(
        "s3", endpoint_url=credentials["endpoint"], region_name=credentials["region"],
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-system"]
    raw = json.loads(subprocess.check_output(kubectl + ["get", "secret", "fs2-serve-admin", "-o", "json"]))
    admin_token = base64.b64decode(raw["data"]["token"]).decode().strip()
    keys, users, objects = [], [], []
    evidence = {"started_at": datetime.now(UTC).isoformat(), "checks": [], "inventory": []}

    def passed(name):
        evidence["checks"].append(name)
        print(json.dumps({"passed": name}), flush=True)

    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False) as admin:
        def request(method, path, payload=None, expected=200):
            response = admin.request(method, path, json=payload)
            require(response.status_code == expected, f"admin_{method}_{path}_{response.status_code}")
            return response.json()["data"]

        def credentials(user):
            deadline = time.monotonic() + 480
            path = f"/admin/api/v1/users/{user['id']}/storage"
            while time.monotonic() < deadline:
                state = request("GET", path)
                if state["state"] == "ready":
                    response = admin.post(path + "/credentials")
                    require(response.status_code == 200, "credential_disclosure")
                    require(response.headers.get("cache-control") == "no-store", "secret_no_store")
                    return response.json()["data"]
                time.sleep(5)
            raise AssertionError("storage_provisioning_timeout")

        def fixture_user(principal):
            listing = request("GET", f"/admin/api/v1/users?tenant_id={FIXTURE_TENANT}&limit=1000")
            found = [u for u in listing["items"] if u["principal_id"] == principal]
            if found:
                user = request("PATCH", f"/admin/api/v1/users/{found[0]['id']}", {"enabled": True})
            else:
                user = request("POST", "/admin/api/v1/users", {
                    "tenant_id": FIXTURE_TENANT, "principal_id": principal,
                    "display_name": "Storage acceptance " + principal, "kind": "service", "app_ids": [],
                }, 201)
            users.append(user)
            return user

        def temporary_key(user):
            value = request("POST", f"/admin/api/v1/users/{user['id']}/keys", {
                "name": "storage-acceptance-" + str(uuid4()),
                "tenant_id": user["tenant_id"], "principal_id": user["principal_id"],
                "models": ["*"], "scopes": ["catalog.read"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }, 201)
            keys.append(value["key"]["id"])
            return value["secret"]

        try:
            login = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + admin_token})
            require(login.status_code == 200, "admin_login")
            listing = request("GET", "/admin/api/v1/users?limit=1000")
            require(not listing["truncated"], "inventory_truncated")
            retained = {}
            for user in listing["items"]:
                if user["tenant_id"] == FIXTURE_TENANT or not user["active_key_count"]:
                    continue
                path = f"/admin/api/v1/users/{user['id']}"
                if user["tenant_id"] == "stockholm":
                    state = request("GET", path + "/storage")
                    require(state["state"] == "disabled" and not state.get("bucket_name"), "stockholm_excluded")
                    require(admin.post(path + "/storage/credentials").status_code == 403, "stockholm_no_secret")
                    continue
                value = credentials(user)
                detail = request("GET", path)
                require("secret_access_key" not in json.dumps(detail), "ordinary_user_detail_no_secret")
                require(detail["storage"]["quota_bytes"] == 5_000_000_000, "five_gb_quota")
                require(detail["storage"]["bucket_name"] == value["bucket_name"], "user_metadata_matches")
                retained[(user["tenant_id"], user["principal_id"])] = value
                evidence["inventory"].append({"tenant": user["tenant_id"], "principal": user["principal_id"],
                                              "bucket": value["bucket_name"], "quota_bytes": 5_000_000_000})
            passed("all_active_users_ready_and_stockholm_excluded")

            rene, kopra = retained[("rene", "rene")], retained[("kopra", "kopra")]
            for own, other in [(rene, kopra), (kopra, rene)]:
                client = s3(own)
                denied(lambda: client.list_objects_v2(Bucket=other["bucket_name"], MaxKeys=1))
            passed("cross_tenant_s3_access_denied")

            shared = [c for (tenant, _), c in retained.items() if tenant == "tenant-academic"]
            require(len(shared) == 2, "shared_fixture_users")
            require(shared[0]["bucket_name"] == shared[1]["bucket_name"], "shared_bucket")
            require(shared[0]["access_key_id"] != shared[1]["access_key_id"], "distinct_user_keys")
            a, b = map(s3, shared)
            key, bucket = "acceptance/customer-storage/" + str(uuid4()), shared[0]["bucket_name"]
            objects.append((a, bucket, key))
            payload = os.urandom(17 * 1024 * 1024)
            a.upload_fileobj(io.BytesIO(payload), bucket, key, Config=TransferConfig(
                multipart_threshold=5 * 1024 * 1024, multipart_chunksize=5 * 1024 * 1024, max_concurrency=4))
            received = b.get_object(Bucket=bucket, Key=key)["Body"].read()
            require(hashlib.sha256(received).digest() == hashlib.sha256(payload).digest(), "multipart_checksum")
            passed("shared_bucket_distinct_keys_and_17_mib_multipart_roundtrip")

            request("PUT", f"/admin/api/v1/tenants/{FIXTURE_TENANT}/storage", {
                "mode": "user", "quota_bytes": 5_000_000_000,
            })
            alice, bob = fixture_user("alice"), fixture_user("bob")
            private_a, private_b = credentials(alice), credentials(bob)
            require(private_a["bucket_name"] != private_b["bucket_name"], "separate_user_buckets")
            a, b = s3(private_a), s3(private_b)
            key, bucket = "acceptance/customer-storage/" + str(uuid4()), private_a["bucket_name"]
            objects.append((a, bucket, key))
            a.put_object(Bucket=bucket, Key=key, Body=b"private acceptance fixture")
            denied(lambda: b.get_object(Bucket=bucket, Key=key))
            denied(lambda: b.put_object(Bucket=bucket, Key=key, Body=b"must not overwrite"))
            require(a.get_object(Bucket=bucket, Key=key)["Body"].read() == b"private acceptance fixture", "denied_write_unchanged")
            passed("per_user_mode_and_same_tenant_read_write_isolation")

            for token in [temporary_key(alice), temporary_key(alice)]:
                with httpx.Client(base_url=args.origin, headers={"authorization": "Bearer " + token}, timeout=60, trust_env=False) as public:
                    info = public.get("/v1/storage")
                    require(info.status_code == 200 and info.json()["bucket_name"] == bucket, "public_own_storage")
                    disclosed = public.post("/v1/storage/credentials")
                    require(disclosed.status_code == 200 and disclosed.json() == private_a, "same_user_keys_same_s3_identity")
                    require(disclosed.headers.get("cache-control") == "no-store", "public_secret_no_store")
                    require(public.get(f"/admin/api/v1/users/{bob['id']}/storage").status_code == 401, "inference_key_not_admin")
            passed("public_authenticated_storage_and_multiple_keys_same_identity")
            for client, object_bucket, object_key in objects:
                client.delete_object(Bucket=object_bucket, Key=object_key)
            objects.clear()
            for user in users:
                request("PATCH", f"/admin/api/v1/users/{user['id']}", {"enabled": False})
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                states = [request("GET", f"/admin/api/v1/users/{u['id']}/storage")["state"] for u in users]
                if all(state == "disabled" for state in states):
                    break
                time.sleep(5)
            require(all(state == "disabled" for state in states), "disabled_users_reconciled")
            denied(lambda: a.list_objects_v2(Bucket=bucket, MaxKeys=1))
            passed("disabled_users_lose_s3_access")
            evidence["outcome"] = "passed"
        except Exception as error:
            evidence["outcome"] = "failed"
            evidence["error_type"] = type(error).__name__
            if isinstance(error, AssertionError):
                evidence["error_code"] = str(error)
        finally:
            for client, bucket, key in objects:
                client.delete_object(Bucket=bucket, Key=key)
            for user in users:
                request("PATCH", f"/admin/api/v1/users/{user['id']}", {"enabled": False})
            for key in keys:
                request("DELETE", f"/admin/api/v1/keys/{key}")
            admin.delete("/admin/api/v1/session")
        evidence["completed_at"] = datetime.now(UTC).isoformat()
        print(json.dumps(evidence, sort_keys=True), flush=True)
        return 0 if evidence["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
