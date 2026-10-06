"""Independently verify internal acceptance outputs in the assigned S3 bucket."""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
import httpx2
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lynx-demo-resume-20261005"))
from run_demo_resume import save  # noqa: E402
from validate_resume import verify_customer_storage  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", required=True, type=Path)
    parser.add_argument("cohorts", nargs="+", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    values = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    with httpx2.Client(
        base_url="https://89.169.99.188",
        trust_env=False,
        timeout=60,
        headers={"Authorization": "Bearer " + values["SCIENTIFIC_MODELS_API_KEY"]},
    ) as client:
        response = client.get("/v1/me")
        response.raise_for_status()
        policy = response.json()
        if (policy["tenant_id"], policy["principal_id"]) != ("system", "qa"):
            raise ValueError("Internal verification must use system/qa")
        response = client.post("/v1/storage/credentials")
        response.raise_for_status()
        storage = response.json()
    s3 = boto3.client(
        "s3",
        endpoint_url=storage["endpoint"],
        region_name=storage["region"],
        aws_access_key_id=storage["access_key_id"],
        aws_secret_access_key=storage["secret_access_key"],
        config=Config(max_pool_connections=4, retries={"mode": "standard", "max_attempts": 3}),
    )
    try:
        for cohort in args.cohorts:
            receipt = json.loads((cohort / "receipt.json").read_text())
            checkpoint = json.loads((cohort / "final-checkpoint.json").read_text())
            if receipt["resume_operation"] != checkpoint["state"]["operation_id"]:
                raise ValueError("Export does not belong to the validated continuation")
            verified = verify_customer_storage(s3, checkpoint, expected_bucket=storage["bucket_name"])
            save(cohort / "independent-bucket-verification.json", verified)
            print(json.dumps({"cohort": cohort.name, **verified}), flush=True)
    finally:
        s3.close()


if __name__ == "__main__":
    main()
