"""Copy the authorized immutable Lynx checkpoint into the existing demo workspace.

Preview is read-only. Apply reuses the existing lifecycle key issuer and storage
disclosure API. It never writes the source bucket, changes a quota, overwrites a
destination object, submits inference as Lynx, or puts customer data in Git.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx2

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
ORIGIN = "https://89.169.99.188"
DEMO_USER = "d878fe0c-810f-5f18-8594-5a1c3a92bf0a"
DEMO_BUCKET = "fs2-demo-user-7cf4fbaf2a81493b"
SOURCE_BUCKET = "fs2-lynx-c327dcc386444425"
SOURCE_OPERATION = "aa502153-3c75-422c-8040-82461fdbfcaa"
SOURCE_PREFIX = f"runs/mas1-20e-production-r3/{SOURCE_OPERATION}/"
SOURCE_KEY = SOURCE_PREFIX + "mas1-20e/attempt-001/checkpoint-00000071.json"
SOURCE_SHA = "cec7c0cd5a7df721dfb61c192c05bc55884ae5dd32d3ea09ca8aea52e41350bb"
TARGET_PREFIX = "runs/fs2-lynx-final-20261005/source"
KEY_NAME = "lynx-resume-qualification-20261005"
SCOPES = ["artifacts.write", "catalog.read", "inference.invoke", "mcp.invoke",
          "operations.acknowledge", "operations.cancel", "operations.read", "operations.result"]
LIFECYCLE = Path("/home/tux/nebius-solutions-library-inference/k8s-inference/operations/tenant-lifecycle/lifecycle.py")


def lifecycle():
    spec = importlib.util.spec_from_file_location("existing_tenant_lifecycle", LIFECYCLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or str(path) != value
            or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value):
        raise ValueError("unsafe checkpoint path")
    return value


def validate_manifest(raw: bytes) -> dict:
    if sha(raw) != SOURCE_SHA:
        raise ValueError("source manifest changed")
    manifest = json.loads(raw)
    state = manifest["state"]
    if (manifest["bucket"] != SOURCE_BUCKET or state["operation_id"] != SOURCE_OPERATION
            or state["generation"] != 71 or state["job_id"] != "mas1-20e"
            or max(c.get("checkpoint_step") or 0 for c in state["commands"]) != 13_963_440):
        raise ValueError("source checkpoint identity changed")
    seen = set()
    for entry in manifest["files"]:
        path = safe_path(entry["path"])
        if path in seen or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
            raise ValueError("duplicate path or invalid digest")
        if (not entry["key"].startswith(SOURCE_PREFIX + "mas1-20e/objects/")
                or entry["key"].rsplit("/", 1)[-1] != entry["sha256"]
                or not isinstance(entry["size_bytes"], int) or entry["size_bytes"] < 0):
            raise ValueError("unexpected source object")
        seen.add(path)
    if len(seen) != 305 or sum(f["size_bytes"] for f in manifest["files"]) != 82_674_112:
        raise ValueError("source inventory changed")
    return manifest


def s3(credentials: dict):
    return boto3.client("s3", endpoint_url=credentials["endpoint"], region_name=credentials["region"],
                        aws_access_key_id=credentials["access_key_id"],
                        aws_secret_access_key=credentials["secret_access_key"],
                        config=Config(max_pool_connections=8, retries={"max_attempts": 3, "mode": "standard"}))


def verified_get(client, bucket: str, key: str, size: int, digest: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        if response["ContentLength"] != size:
            raise ValueError("object length changed")
        data = response["Body"].read(size + 1)
    finally:
        response["Body"].close()
    if len(data) != size or sha(data) != digest:
        raise ValueError("object checksum mismatch")
    return data


def put_once(client, key: str, data: bytes) -> None:
    try:
        client.put_object(Bucket=DEMO_BUCKET, Key=key, Body=data, IfNoneMatch="*",
                          Metadata={"sha256": sha(data), "purpose": "authorized-private-lynx-resume-test"})
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
    verified_get(client, DEMO_BUCKET, key, len(data), sha(data))


class Admin:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, body=None):
        response = self.client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()["data"]


def main(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    helper = lifecycle()
    secret = json.loads(subprocess.check_output([
        "/snap/bin/kubectl", "--context", CONTEXT, "--request-timeout=20s", "-n", "fs2-system",
        "get", "secret", "fs2-serve-admin", "-o", "json"], timeout=30))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    source_credentials = json.loads(args.source_credentials.read_text())
    if source_credentials["bucket_name"] != SOURCE_BUCKET:
        raise ValueError("source credential belongs to a different bucket")
    source = s3(source_credentials)
    raw = verified_get(source, SOURCE_BUCKET, SOURCE_KEY, 130_375, SOURCE_SHA)
    manifest = validate_manifest(raw)
    with httpx2.Client(base_url=ORIGIN, timeout=60, trust_env=False,
                       headers={"Origin": ORIGIN, "X-Requested-With": "XMLHttpRequest"}) as client:
        client.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token}).raise_for_status()
        admin = Admin(client)
        try:
            user = helper.select_user(admin, "demo-user", "demo-user")
            if user["id"] != DEMO_USER or not user["enabled"]:
                raise ValueError("existing demo identity changed")
            storage = admin.request("GET", f"/admin/api/v1/users/{DEMO_USER}/storage")
            if storage["state"] != "ready" or storage["bucket_name"] != DEMO_BUCKET:
                raise ValueError("existing demo bucket changed")
            destination_credentials = admin.request("POST", f"/admin/api/v1/users/{DEMO_USER}/storage/credentials")
            if destination_credentials["bucket_name"] != DEMO_BUCKET:
                raise ValueError("destination disclosure mismatch")
            destination = s3(destination_credentials)
            used_bytes = sum(obj["Size"] for page in destination.get_paginator("list_objects_v2").paginate(
                Bucket=DEMO_BUCKET) for obj in page.get("Contents", []))
            copy_bytes = len(raw) + sum(f["size_bytes"] for f in manifest["files"])
            if used_bytes + copy_bytes + 2_000_000_000 > storage["quota_bytes"]:
                raise ValueError("demo headroom inadequate for copy plus 2 GB acceptance allowance")
            plan = {"source_operation": SOURCE_OPERATION, "source_manifest_sha256": SOURCE_SHA,
                    "source_bucket": SOURCE_BUCKET, "source_files": len(manifest["files"]),
                    "source_bytes": copy_bytes, "source_step": 13_963_440,
                    "demo_user_id": DEMO_USER, "demo_tenant": "demo-user", "demo_bucket": DEMO_BUCKET,
                    "destination_prefix": TARGET_PREFIX, "demo_existing_bytes": used_bytes,
                    "demo_quota_bytes": storage["quota_bytes"], "apply": args.apply,
                    "source_writes": False, "customer_inference_key_used": False,
                    "canonical_seed_changed": False, "quota_changed": False}
            plan_path = args.output / ("apply-plan.json" if args.apply else "preview.json")
            helper.private_output(plan_path, plan)
            print(json.dumps(plan), flush=True)
            if not args.apply:
                return
            credentials_path = args.output / "demo-storage.json"
            if not credentials_path.exists():
                helper.private_output(credentials_path, destination_credentials)
            key_path = args.output / "demo-api-key.json"
            if not key_path.exists():
                helper.issue_key(admin, "demo-user", "demo-user", {
                    "name": KEY_NAME, "models": ["gromacs"], "scopes": SCOPES,
                    "max_concurrency": 2, "rate_limit_requests": 50, "rate_window_seconds": 86400,
                }, key_path)
            key = json.loads(key_path.read_text())["key"]
            if key["tenant_id"] != "demo-user" or key["name"] != KEY_NAME:
                raise ValueError("retained test key identity mismatch")
            native = args.output / "source" / "native"
            native.mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest_path = args.output / "source" / "customer-manifest.json"
            if not manifest_path.exists():
                with manifest_path.open("xb") as stream:
                    stream.write(raw)
            elif manifest_path.read_bytes() != raw:
                raise ValueError("local source manifest changed")

            def copy_one(entry):
                data = verified_get(source, SOURCE_BUCKET, entry["key"], entry["size_bytes"], entry["sha256"])
                local = native / safe_path(entry["path"])
                local.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if local.exists():
                    if local.is_symlink() or local.read_bytes() != data:
                        raise ValueError("local checkpoint data differs")
                else:
                    with local.open("xb") as stream:
                        stream.write(data)
                target = f"{TARGET_PREFIX}/native/{entry['path']}"
                put_once(destination, target, data)
                return {"path": entry["path"], "sha256": entry["sha256"],
                        "size_bytes": len(data), "destination_key": target}

            rows = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                for future in as_completed([pool.submit(copy_one, entry) for entry in manifest["files"]]):
                    rows.append(future.result())
                    if len(rows) % 50 == 0:
                        print(json.dumps({"copied_and_download_verified_files": len(rows)}), flush=True)
            put_once(destination, TARGET_PREFIX + "/customer-manifest.json", raw)
            receipt = {**plan, "verified": True, "demo_key_id": key["id"],
                       "files": sorted(rows, key=lambda row: row["path"])}
            helper.private_output(args.output / "copy-receipt.json", receipt)
            print(json.dumps({"verified": True, "files": len(rows), "bytes": copy_bytes,
                              "receipt": str(args.output / "copy-receipt.json"), "demo_key_id": key["id"]}), flush=True)
        finally:
            client.delete("/admin/api/v1/session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
