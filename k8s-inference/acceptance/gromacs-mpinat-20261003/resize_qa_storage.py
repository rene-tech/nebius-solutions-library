"""Owner-approved 100 GB limit on the existing shared system benchmark bucket.

Use the admin storage policy so reconciliation preserves the change. No direct
database/provider writes, credential rotation, customer change or data deletion.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import boto3
from botocore.config import Config
import httpx2

BUCKET = "fs2-system-35f1ad07f2fe32cf"
TARGET = 100_000_000_000
ORIGIN = "https://89.169.99.188"
CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"


def main(a):
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        (a.output / name).write_text(json.dumps(value, indent=2) + "\n")
    def cloud():
        raw = subprocess.check_output(["nebius", "--profile", "sandbox2", "--no-browser", "--format", "json",
            "storage", "bucket", "get-by-name", "--parent-id", "project-e00rene", "--name", BUCKET], timeout=60)
        return json.loads(raw)
    key = dict(line.split("=", 1) for line in a.qa_env.read_text().splitlines() if "=" in line)["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only existing system/qa")
    secret = json.loads(subprocess.check_output(["kubectl", "--context", CONTEXT, "--request-timeout=20s",
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx2.Client(base_url=ORIGIN, headers={"Authorization": "Bearer " + key}, timeout=60, trust_env=False) as own, \
         httpx2.Client(base_url=ORIGIN, headers={"Origin": ORIGIN, "X-Requested-With": "XMLHttpRequest"}, timeout=60, trust_env=False) as admin:
        response = admin.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        try:
            response = own.get("/v1/storage"); response.raise_for_status(); view = response.json()
            if view["mode"] != "tenant" or view["bucket_name"] != BUCKET or view["state"] != "ready":
                raise ValueError("QA workspace is not the approved shared bucket")
            before = cloud()
            save("provider-before.json", before)
            response = admin.get("/admin/api/v1/tenants/system/storage"); response.raise_for_status()
            policy = response.json()["data"]
            if policy["mode"] != "tenant" or policy["quota_bytes"] not in (5_000_000_000, TARGET):
                raise ValueError("Unexpected policy; inspect before changing it")
            save("policy-before.json", policy)
            if policy["quota_bytes"] != TARGET:
                response = admin.put("/admin/api/v1/tenants/system/storage", json={**policy, "quota_bytes": TARGET})
                response.raise_for_status(); save("policy-update.json", response.json())
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = own.get("/v1/storage"); response.raise_for_status(); after_view = response.json()
                if after_view["quota_bytes"] == TARGET:
                    after = cloud()
                    if int(after["spec"]["max_size_bytes"]) == TARGET:
                        break
                time.sleep(5)
            else:
                raise RuntimeError("Policy changed but provider reconciliation not yet verified")
            if before["metadata"]["id"] != after["metadata"]["id"]:
                raise ValueError("Bucket identity changed")
            changes = {k for k in set(before["spec"]) | set(after["spec"]) if before["spec"].get(k) != after["spec"].get(k)}
            if changes - {"max_size_bytes"}:
                raise ValueError("Unexpected provider spec changes")
            save("provider-after.json", after)
            response = own.post("/v1/storage/credentials"); response.raise_for_status(); credentials = response.json()
            if credentials["bucket_name"] != BUCKET:
                raise ValueError("Credential disclosure belongs to another bucket")
            s3 = boto3.client("s3", endpoint_url=credentials["endpoint"], region_name=credentials["region"],
                aws_access_key_id=credentials["access_key_id"], aws_secret_access_key=credentials["secret_access_key"],
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
            payload = b'{"task":"fs2-gromacs-mpinat-api-mcp-mpi-r20261003","check":"owner-approved-100GB-read-write"}\n'
            object_key = "runs/fs2-mpinat-storage-20261003/100gb-read-write-check.json"
            started = time.monotonic()
            s3.put_object(Bucket=BUCKET, Key=object_key, Body=payload, ContentType="application/json")
            write_seconds = time.monotonic() - started
            started = time.monotonic()
            returned = s3.get_object(Bucket=BUCKET, Key=object_key)["Body"].read()
            read_seconds = time.monotonic() - started
            if returned != payload:
                raise ValueError("S3 read-after-write mismatch")
            receipt = {"bucket_name": BUCKET, "bucket_id": after["metadata"]["id"], "quota_bytes": TARGET,
                "previous_quota_bytes": int(before["spec"]["max_size_bytes"]), "verified": True,
                "object_key": object_key, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "write_seconds": write_seconds, "read_seconds": read_seconds, "customer_changed": False,
                "project_quota_changed": False, "objects_deleted": False}
            save("verification.json", receipt)
            print(json.dumps(receipt))
        finally:
            admin.delete("/admin/api/v1/session")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qa-env", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    main(p.parse_args())
