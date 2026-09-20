#!/usr/bin/env python3
"""Customer-shaped workspace seeding checks; never reads unrelated object bodies.

prepare creates two task-owned users (one shared peer, one private workspace).
verify downloads only the pinned examples prefix using each customer's key.
delete-canary removes exactly one installed README in our own test bucket,
retaining a local backup; deletion-check proves it is not silently restored.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

SHARED = "fs2-starter-data-acceptance-20260920"
PRIVATE = "fs2-starter-private-20260920"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main(args):
    access = json.loads(args.key_file.read_bytes())
    assert access["tenant_id"] in {SHARED, PRIVATE}
    if args.action == "prepare":
        assert access["tenant_id"] == SHARED
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
        token = (
            base64.b64decode(
                json.loads(subprocess.check_output(command))["data"]["token"]
            )
            .decode()
            .strip()
        )
        with httpx.Client(
            base_url=access["origin"], headers={"origin": access["origin"]}, timeout=60
        ) as admin:
            admin.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            ).raise_for_status()

            def call(method, path, value=None):
                response = admin.request(method, path, json=value)
                response.raise_for_status()
                return response.json()["data"]

            users = call("GET", "/admin/api/v1/users?limit=1000")["items"]
            # Only this fresh task-owned tenant may receive a private policy.
            policy = call("GET", f"/admin/api/v1/tenants/{PRIVATE}/storage")
            existing = [u for u in users if u["tenant_id"] == PRIVATE]
            if existing:
                assert policy["mode"] == "user", (
                    "existing_private_fixture_policy_changed"
                )
            else:
                call(
                    "PUT",
                    f"/admin/api/v1/tenants/{PRIVATE}/storage",
                    {"mode": "user", "quota_bytes": 5000000000},
                )
            records = []
            for tenant, principal in [
                (SHARED, "shared-peer"),
                (PRIVATE, "private-canary"),
            ]:
                matches = [
                    u
                    for u in users
                    if u["tenant_id"] == tenant and u["principal_id"] == principal
                ]
                user = (
                    matches[0]
                    if matches
                    else call(
                        "POST",
                        "/admin/api/v1/users",
                        {
                            "tenant_id": tenant,
                            "principal_id": principal,
                            "display_name": "Starter-data workspace acceptance",
                            "kind": "service",
                            "app_ids": None if tenant == PRIVATE else [],
                        },
                    )
                )
                assert user["enabled"], "do_not_reactivate_fixture"
                records.append(
                    {k: user[k] for k in ("id", "tenant_id", "principal_id")}
                )
                if tenant == PRIVATE and not args.private_key_file.exists():
                    disclosure = call(
                        "POST",
                        f"/admin/api/v1/users/{user['id']}/keys",
                        {
                            "name": "starter-private-canary",
                            "tenant_id": tenant,
                            "principal_id": principal,
                            "models": ["qwen3-8b", "sam2-1-hiera-large", "openfold2"],
                            "scopes": [
                                "catalog.read",
                                "inference.invoke",
                                "mcp.invoke",
                                "artifacts.write",
                                "operations.read",
                                "operations.result",
                                "operations.cancel",
                            ],
                            "max_concurrency": 2,
                            "request_budget": 50,
                            "expires_at": (
                                datetime.now(UTC) + timedelta(days=2)
                            ).isoformat(),
                        },
                    )
                    fd = os.open(
                        args.private_key_file,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(fd, "w") as file:
                        json.dump(
                            {
                                "origin": access["origin"],
                                "tenant_id": tenant,
                                "principal_id": principal,
                                "user_id": user["id"],
                                "key_id": disclosure["key"]["id"],
                                "secret": disclosure["secret"],
                            },
                            file,
                        )
            save(
                args.output, {"at": datetime.now(UTC).isoformat(), "fixtures": records}
            )
            print(json.dumps({"fixtures": records}))
        return
    with httpx.Client(
        base_url=access["origin"],
        headers={"authorization": "Bearer " + access["secret"]},
        timeout=60,
    ) as api:
        response = api.get("/v1/storage")
        response.raise_for_status()
        state = response.json()
        report = {
            "at": datetime.now(UTC).isoformat(),
            "tenant_id": access["tenant_id"],
            "principal_id": access["principal_id"],
            "bucket": state["bucket_name"],
            "storage_state": state["state"],
            "examples": state["examples"],
        }
        if args.action == "status":
            save(args.output, report)
            print(json.dumps(report))
            return
        assert state["state"] == "ready" and state["examples"]["state"] == "complete", (
            "seeding_not_complete"
        )
        response = api.post("/v1/storage/credentials")
        response.raise_for_status()
        assert response.headers.get("cache-control") == "no-store"
        credentials = response.json()
    client = boto3.client(
        "s3",
        endpoint_url=credentials["endpoint"],
        region_name=credentials["region"],
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2},
            max_pool_connections=8,
        ),
    )
    assert credentials["bucket_name"] == state["bucket_name"]
    bucket = state["bucket_name"]

    def read(key):
        response = client.get_object(Bucket=bucket, Key=key)
        try:
            return response["Body"].read(128 * 1024 * 1024 + 1)
        finally:
            response["Body"].close()

    if args.action == "delete-canary":
        assert access["tenant_id"] == SHARED and access["principal_id"] == "seed-canary"
        assert bucket == "fs2-fs2-starter-data-acceptance-20260920-0164f70008da5227"
        data = read("examples/v1/README.md")
        backup = args.output.with_suffix(".README.backup.md")
        assert not backup.exists(), "deletion_probe_already_started"
        backup.write_bytes(data)
        client.delete_object(Bucket=bucket, Key="examples/v1/README.md")
        report.update(
            deleted_key="examples/v1/README.md",
            sha256=hashlib.sha256(data).hexdigest(),
            recoverable_from="pinned pack and task-local backup",
        )
    elif args.action == "deletion-check":
        try:
            read("examples/v1/README.md")
        except ClientError as error:
            assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        else:
            raise AssertionError("customer_deleted_example_was_restored")
        report["deleted_example_not_restored"] = True
    else:
        manifest = (args.pack / "manifest.json").read_bytes()
        assert read("examples/v1/manifest.json") == manifest
        value = json.loads(manifest)
        assert (
            state["examples"]["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
        )

        def verify(item):
            body = read("examples/v1/" + item["path"])
            assert (
                len(body) == item["size_bytes"]
                and hashlib.sha256(body).hexdigest() == item["sha256"]
            )
            if args.download:
                path = args.download / item["path"]
                assert path.resolve().is_relative_to(args.download.resolve())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            return len(body)

        if args.download:
            args.download.mkdir(parents=True, exist_ok=False)
            (args.download / "manifest.json").write_bytes(manifest)
        with ThreadPoolExecutor(max_workers=8) as workers:
            sizes = list(workers.map(verify, value["objects"]))
        report.update(
            verified_objects=len(sizes) + 1, verified_bytes=sum(sizes) + len(manifest)
        )
    client.close()
    save(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["prepare", "status", "verify", "delete-canary", "deletion-check"],
    )
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key-file", type=Path)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context")
    main(parser.parse_args())
