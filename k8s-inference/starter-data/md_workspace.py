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


def verify_exports(s3, bucket, access, runs):
    """Read the actual customer copies, not merely the platform artifact store."""
    verified = []
    for run in runs:
        receipt = json.loads((run / "receipt.json").read_bytes())
        identity = receipt["identity"]
        if (
            receipt["state"] != "succeeded"
            or identity["caller_sha256"] != runner.sha(access["secret"].encode())
            or not identity["case_id"].startswith("molecular-dynamics/")
        ):
            raise ValueError("completed_owned_md_run_required")
        native = []
        for item in receipt["artifacts"]:
            if item["media_type"] != "application/json":
                continue
            path = run / item["local_path"]
            if runner.file_sha(path) != item["sha256"]:
                raise ValueError("native_receipt_changed")
            value = json.loads(path.read_bytes())
            if value.get("schema", "").endswith("-workflow-result/v1"):
                native.append(value)
        if not native:
            raise ValueError("native_result_missing")
        for result in native:
            prefix = (
                "runs/starter-md/"
                + identity["case_id"].split("/")[-1]
                + "/"
                + identity["model_id"]
                + "/"
                + receipt["operation_id"]
                + "/"
                + result["job_id"]
                + "/"
            )
            checkpoints = []
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix
            ):
                checkpoints.extend(
                    o["Key"]
                    for o in page.get("Contents", [])
                    if "/checkpoint-" in o["Key"] and o["Key"].endswith(".json")
                )
            if not checkpoints:
                raise ValueError("customer_checkpoint_missing")
            response = s3.get_object(Bucket=bucket, Key=max(checkpoints))
            try:
                checkpoint = json.loads(response["Body"].read(16 * 1024**2))
            finally:
                response["Body"].close()
            if checkpoint["state"]["completed_steps"] != result["completed_steps"]:
                raise ValueError("customer_checkpoint_incomplete")
            expected = {
                f["path"]: (f["sha256"], f["size_bytes"]) for f in result["files"]
            }
            exported = {
                f["path"]: (f["sha256"], f["size_bytes"]) for f in checkpoint["files"]
            }
            if exported != expected:
                raise ValueError("customer_export_inventory_differs")

            def check_file(item, operation_prefix=prefix):
                if item["key"] != operation_prefix + "objects/" + item["sha256"]:
                    raise ValueError("customer_export_not_operation_scoped")
                response = s3.get_object(Bucket=bucket, Key=item["key"])
                digest, size = hashlib.sha256(), 0
                try:
                    for chunk in response["Body"].iter_chunks(chunk_size=1024**2):
                        size += len(chunk)
                        if size > item["size_bytes"]:
                            raise ValueError("customer_export_size_mismatch")
                        digest.update(chunk)
                finally:
                    response["Body"].close()
                if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("customer_export_checksum_mismatch")
                return size

            with ThreadPoolExecutor(max_workers=4) as pool:
                sizes = list(pool.map(check_file, checkpoint["files"]))
            verified.append(
                {
                    "case_id": identity["case_id"],
                    "model_id": identity["model_id"],
                    "operation_id": receipt["operation_id"],
                    "job_id": result["job_id"],
                    "objects": len(sizes),
                    "bytes": sum(sizes),
                    "state": "passed",
                }
            )
    return verified


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
    if args.verify_runs:
        report["customer_output_verification"] = verify_exports(
            s3, state["bucket_name"], access, args.verify_runs
        )
    s3.close()
    runner.save(args.output, report)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--verify-runs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
