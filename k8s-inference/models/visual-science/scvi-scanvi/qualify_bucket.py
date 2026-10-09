"""Verify the internal test's tenant-bucket manifests and selected raw bytes.

No buckets, users, keys or policies are created/changed. Disclosed credentials
remain in process memory; only non-secret byte/digest receipts are written.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import boto3
import httpx
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--prefix", default="qualification/scvi-whitelab-20261006")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    env = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    with httpx.Client(base_url="https://89.169.99.188", headers={"Authorization": "Bearer " + env["SCIENTIFIC_MODELS_API_KEY"]}, timeout=60, trust_env=False) as client:
        me = client.get("/v1/me").json()
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Only the existing internal QA workspace is in scope")
        response = client.post("/v1/storage/credentials")
        response.raise_for_status()
        value = response.json()
    s3 = boto3.client("s3", endpoint_url=value["endpoint"], region_name=value["region"],
                      aws_access_key_id=value["access_key_id"], aws_secret_access_key=value["secret_access_key"],
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    bucket = value["bucket_name"]
    value.clear()
    prefix = f"{args.prefix}/{args.operation_id}/main/"
    objects = [item for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix) for item in page.get("Contents", [])]
    manifests = sorted(item["Key"] for item in objects if "/checkpoint-" in item["Key"] and item["Key"].endswith(".json"))
    if not manifests:
        raise ValueError("No customer-bucket checkpoint manifest exists")
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=manifests[-1])["Body"].read())
    if manifest["state"]["operation_id"] != args.operation_id:
        raise ValueError("Wrong operation in the tenant-bucket manifest")
    verified, readback = [], []
    for item in manifest["files"]:
        if not item["key"].startswith(prefix + "objects/"):
            raise ValueError("Unexpected object outside the test's operation prefix")
        head = s3.head_object(Bucket=bucket, Key=item["key"])
        digests = [value for key, value in head.get("Metadata", {}).items() if key.lower() == "sha256"]
        if head["ContentLength"] != item["size_bytes"] or not digests or any(digest != item["sha256"] for digest in digests):
            raise ValueError("Object metadata differs from the committed inventory")
        verified.append(item["path"])
        if item["path"] in {"reference.tar.gz", "preflight.json", "predicted_labels.csv"}:
            stream, digest, size = s3.get_object(Bucket=bucket, Key=item["key"])["Body"], hashlib.sha256(), 0
            for chunk in iter(lambda: stream.read(1024**2), b""):
                digest.update(chunk)
                size += len(chunk)
            stream.close()
            if (size, digest.hexdigest()) != (item["size_bytes"], item["sha256"]):
                raise ValueError("Direct S3 readback did not match the committed bytes")
            readback.append({"path": item["path"], "sha256": digest.hexdigest(), "size_bytes": size})
    result = {"operation_id": args.operation_id, "bucket": bucket, "manifest_key": manifests[-1],
              "generation": manifest["state"]["generation"], "completed_stages": manifest["state"]["completed_stages"],
              "metadata_verified_files": len(verified), "direct_byte_readback": readback}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"operation_id": args.operation_id, "verified_files": len(verified), "byte_readback_files": len(readback)}))


if __name__ == "__main__":
    main()
