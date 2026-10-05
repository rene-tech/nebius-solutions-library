"""Read/hash-verify task-owned customer checkpoints, including failed attempts.

Credential disclosure returns the existing system/qa S3 pair in memory. No
bucket/object/identity mutation or deletion; no credential is printed or saved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
import httpx2

WORKSPACE_PREFIXES = ("runs/fs2-mpinat-", "runs/fs2-lynx-performance-20261005-")


def owned_workspace_prefix(base, allowed):
    if allowed not in WORKSPACE_PREFIXES or not isinstance(base, str):
        return False
    return (base.startswith(allowed) and not Path(base).is_absolute()
            and ".." not in Path(base).parts and "//" not in base)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qa-env", type=Path, required=True)
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--events-only", action="store_true")
    p.add_argument("--workspace-prefix", choices=WORKSPACE_PREFIXES, default=WORKSPACE_PREFIXES[0],
                   help="Exact allowlisted internal campaign family; existing QA identity guard remains mandatory")
    a = p.parse_args()
    os.umask(0o077)
    env = dict(line.split("=", 1) for line in a.qa_env.read_text().splitlines() if "=" in line)
    key = env["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only system/qa")
    with httpx2.Client(base_url="https://89.169.99.188", headers={"Authorization": "Bearer " + key},
                       timeout=60, trust_env=False) as http:
        for receipt_file in sorted(a.cohort.glob("*/receipt.json")):
            receipt = json.loads(receipt_file.read_text())
            operation = receipt.get("operation_id")
            if not operation:
                continue
            events, after = [], 0
            for _ in range(100):
                page = http.get(f"/v1/operations/{operation}/events", params={"after_sequence": after, "limit": 200})
                page.raise_for_status()
                rows = page.json()["data"]
                events.extend(rows)
                if len(rows) < 200:
                    break
                next_after = max(e["sequence"] for e in rows)
                if next_after <= after:
                    raise ValueError("Event pagination did not advance")
                after = next_after
            else:
                raise ValueError("Event pagination exceeded its bound")
            save(receipt_file.parent / "events.json", {"data": events})
        if a.events_only:
            return
        response = http.post("/v1/storage/credentials")
        response.raise_for_status()
        storage = response.json()
    s3 = boto3.client("s3", endpoint_url=storage["endpoint"], region_name=storage["region"],
        aws_access_key_id=storage["access_key_id"], aws_secret_access_key=storage["secret_access_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    bucket = storage["bucket_name"]
    storage.clear()
    for receipt_file in sorted(a.cohort.glob("*/receipt.json")):
        receipt = json.loads(receipt_file.read_text())
        request = json.loads((receipt_file.parent / "request.json").read_text())
        if not receipt.get("operation_id"):
            continue
        base = request["parameters"]["output_prefix"]
        if not owned_workspace_prefix(base, a.workspace_prefix):
            raise ValueError("Refuse unrelated workspace prefixes")
        prefix = base + "/" + receipt["operation_id"] + "/"
        out = a.output / receipt_file.parent.name
        objects = [item for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
                   for item in page.get("Contents", [])]
        save(out / "storage-inventory.json", {"bucket": bucket, "prefix": prefix,
             "observed_at": datetime.now(timezone.utc).isoformat(),
             "objects": [{"key": x["Key"], "bytes": x["Size"], "modified": x["LastModified"].isoformat()} for x in objects],
             "retained_bytes": sum(x["Size"] for x in objects)})
        manifests = [x for x in objects if x["Key"].endswith(".json")]
        for item in manifests:
            raw = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            manifest = json.loads(raw)
            relative = Path(item["Key"][len(prefix):])
            save(out / relative, manifest)
        if not manifests:
            print(json.dumps({"case": receipt_file.parent.name, "manifests": 0}), flush=True)
            continue
        errors, verified = [], []
        seen = set()
        for item in sorted(manifests, key=lambda x: x["Key"]):
            manifest = json.loads((out / item["Key"][len(prefix):]).read_text())
            job_id = manifest["state"]["job_id"]
            if job_id not in {j["id"] for j in request["parameters"]["jobs"]}:
                raise ValueError("Unexpected checkpoint job identity")
            for file in manifest["files"]:
                name = Path(file["path"])
                job_prefix = prefix + job_id + "/objects/"
                if name.is_absolute() or ".." in name.parts or not file["key"].startswith(job_prefix):
                    raise ValueError("Checkpoint escapes owned run prefix")
                if name.suffix not in {".log", ".mdp", ".xvg", ".json"} or file["size_bytes"] > 64 * 1024**2:
                    continue
                version = out / "native-versions" / file["sha256"]
                if file["sha256"] in seen:
                    content = version.read_bytes()
                    elapsed = None
                else:
                    started = time.monotonic()
                    content = s3.get_object(Bucket=bucket, Key=file["key"])["Body"].read()
                    elapsed = time.monotonic() - started
                if len(content) != file["size_bytes"] or hashlib.sha256(content).hexdigest() != file["sha256"]:
                    raise ValueError("Bucket artifact digest/size differs")
                if file["sha256"] not in seen:
                    version.parent.mkdir(parents=True, exist_ok=True)
                    version.write_bytes(content)
                    seen.add(file["sha256"])
                    verified.append({**file, "checkpoint_source": item["Key"],
                                     "download_seconds": elapsed, "local_version": str(version)})
                    text = content.decode(errors="replace")
                    if "Fatal error:" in text or "Error in user input:" in text:
                        errors.append({"file": str(name), "sha256": file["sha256"], "diagnostic": text[-2500:]})
                # Convenient latest view; earlier versions remain hash-addressed.
                target = out / "native" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        save(out / "verified-files.json", verified)
        save(out / "native-errors.json", errors)
        print(json.dumps({"case": receipt_file.parent.name, "manifests": len(manifests),
                          "verified_text_files": len(verified), "native_errors": len(errors)}), flush=True)


if __name__ == "__main__":
    main()
