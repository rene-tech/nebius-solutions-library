#!/usr/bin/env python3
"""Read-only authoritative workspace/backfill evidence, with scoped verification.

Only active workspace records are considered. Optional object reads are limited
to the exact pinned starter-pack keys, never customer uploads or work logs.
Private output contains bucket identities; console prints counts only.
"""

import argparse
import base64
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx
from botocore.config import Config


def main(args):
    token = (
        base64.b64decode(
            json.loads(
                subprocess.check_output(
                    [
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
                )
            )["data"]["token"]
        )
        .decode()
        .strip()
    )
    report = {
        "at": datetime.now(UTC).isoformat(),
        "users": [],
        "buckets": [],
        "verification": [],
    }
    with httpx.Client(
        base_url=args.origin, headers={"origin": args.origin}, timeout=60
    ) as admin:
        admin.post(
            "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
        ).raise_for_status()

        def call(method, path):
            response = admin.request(method, path)
            response.raise_for_status()
            return response.json()["data"]

        users = call("GET", "/admin/api/v1/users?limit=1000")["items"]
        assert len(users) < 1000, "inventory_requires_pagination"
        buckets = {}
        for user in users:
            if not user["enabled"]:
                continue
            state = call("GET", f"/admin/api/v1/users/{user['id']}/storage")
            record = {
                "user_id": user["id"],
                "tenant_id": user["tenant_id"],
                "principal_id": user["principal_id"],
                "mode": state["mode"],
                "state": state["state"],
                "bucket": state.get("bucket_name"),
                "examples": state["examples"],
            }
            report["users"].append(record)
            if state["state"] == "ready":
                buckets.setdefault(state["bucket_name"], record)
        report["buckets"] = list(buckets.values())
        if args.verify:
            manifest_bytes = (args.pack / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            for bucket, record in buckets.items():
                # The original v1 deletion probe intentionally removed one file.
                if manifest["version"] == "v1" and record["tenant_id"].startswith(
                    "fs2-starter-"
                ):
                    continue
                if (
                    record["examples"]["state"] != "complete"
                    or record["examples"].get("version") != manifest["version"]
                    or record["examples"].get("manifest_sha256")
                    != hashlib.sha256(manifest_bytes).hexdigest()
                ):
                    report["verification"].append(
                        {"bucket": bucket, "state": "not-complete"}
                    )
                    continue
                credentials = call(
                    "POST",
                    f"/admin/api/v1/users/{record['user_id']}/storage/credentials",
                )
                assert credentials["bucket_name"] == bucket
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

                def verify(item):
                    response = client.get_object(
                        Bucket=bucket,
                        Key=f"examples/{manifest['version']}/" + item["path"],
                    )
                    try:
                        digest, size = hashlib.sha256(), 0
                        while chunk := response["Body"].read(1024 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                    finally:
                        response["Body"].close()
                    assert (
                        size == item["size_bytes"]
                        and digest.hexdigest() == item["sha256"]
                    ), "example_checksum_mismatch"
                    return size

                objects = manifest["objects"] + [
                    {
                        "path": "manifest.json",
                        "size_bytes": len(manifest_bytes),
                        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    }
                ]
                with ThreadPoolExecutor(max_workers=8) as pool:
                    sizes = list(pool.map(verify, objects))
                client.close()
                report["verification"].append(
                    {
                        "bucket": bucket,
                        "state": "passed",
                        "objects": len(sizes),
                        "bytes": sum(sizes),
                    }
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "at": report["at"],
                "active_users": len(report["users"]),
                "disabled_storage_users": sum(
                    u["state"] == "disabled" for u in report["users"]
                ),
                "ready_buckets": len(report["buckets"]),
                "examples": {
                    s: sum(b["examples"]["state"] == s for b in report["buckets"])
                    for s in sorted({b["examples"]["state"] for b in report["buckets"]})
                },
                "independently_verified_buckets": sum(
                    v["state"] == "passed" for v in report["verification"]
                ),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--verify", action="store_true")
    main(parser.parse_args())
