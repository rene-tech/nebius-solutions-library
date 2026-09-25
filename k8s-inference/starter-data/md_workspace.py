#!/usr/bin/env python3
"""Inspect/download this MD canary's workspace with its own public API key.

No admin credential, project storage key, bucket creation or customer-data writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import httpx2 as httpx
import run_example as runner
from botocore.config import Config
from md_acceptance import TENANTS, checked


def main(args):
    os.umask(0o077)
    access = json.loads(args.access.read_bytes())
    if access["tenant_id"] not in TENANTS or not access.get("disposable"):
        raise ValueError("canary_access_required")
    with httpx.Client(
        base_url=access["origin"],
        timeout=60,
        trust_env=False,
        headers={"authorization": "Bearer " + access["secret"]},
    ) as api:
        state = checked(api.get("/v1/storage"))
        credentials = checked(api.post("/v1/storage/credentials"))
    if credentials["bucket_name"] != state["bucket_name"]:
        raise ValueError("storage_identity_changed")
    s3 = boto3.client(
        "s3",
        endpoint_url=credentials["endpoint"],
        region_name=credentials["region"],
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            max_pool_connections=8,
            retries={"max_attempts": 3},
        ),
    )
    total, objects = 0, 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=state["bucket_name"]
    ):
        for item in page.get("Contents", []):
            total += item["Size"]
            objects += 1
    report = {
        "state": state["state"],
        "examples": state.get("examples"),
        "objects": objects,
        "usage_bytes": total,
        "quota_bytes": state["quota_bytes"],
        "key_id": access["key_id"],
    }
    if args.pack:
        raw = (args.pack / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if state.get("examples", {}).get("manifest_sha256") != runner.sha(raw):
            raise ValueError("seeded_manifest_not_the_candidate")
        if args.download:
            args.download.mkdir(parents=True, exist_ok=False)

        def verify(item):
            response = s3.get_object(
                Bucket=state["bucket_name"],
                Key=f"examples/{manifest['version']}/" + item["path"],
            )
            try:
                content = response["Body"].read(item["size_bytes"] + 1)
            finally:
                response["Body"].close()
            if (
                len(content) != item["size_bytes"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise ValueError("seeded_object_checksum_mismatch")
            if args.download:
                target = args.download / item["path"]
                if not target.resolve().is_relative_to(args.download.resolve()):
                    raise ValueError("invalid_seed_path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return len(content)

        entries = manifest["objects"] + [
            {"path": "manifest.json", "size_bytes": len(raw), "sha256": runner.sha(raw)}
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(verify, entries))
        report.update(
            verified_objects=len(counts),
            verified_bytes=sum(counts),
            manifest_sha256=runner.sha(raw),
            downloaded_to=str(args.download) if args.download else None,
        )
    s3.close()
    runner.save(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
