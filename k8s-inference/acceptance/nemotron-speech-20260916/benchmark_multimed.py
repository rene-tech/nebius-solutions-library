"""Reproducible German medical ASR evaluation on a task-owned resident worker.

Run with pyarrow==21.0.0, httpx==0.28.1 and boto3==1.40.30. Download only the
pinned German test parquet from leduckhai/MultiMed. The publisher marks it MIT;
audio/transcripts are research benchmark assets, not bundled platform content.
All 1091 test rows are scored. A fixed, evenly spaced 30-row subset gets three
ADDITIONAL warm repetitions. Never treat those repeated rows as extra quality
samples. Signed URLs/credentials exist only in memory and kubectl stdin.
"""

import argparse
import base64
import hashlib
import json
import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import boto3
import httpx
import pyarrow.parquet as pq
from botocore.config import Config

from score_medical import alignment, words

REVISION = "459d0ab6db332904f9d7b76a8baabf3333958fa8"
PARQUET_SHA256 = "494a635916ceaed914f6238fb7acf37e38a1e8432c30663a2f6f484dbdec58e0"
MODEL = "nemotron-speech-multilingual-0.6b"

DRIVER = r'''
import json, sys, time
import httpx
batch = json.load(sys.stdin)
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False) as client:
    ready = client.get("/readyz")
    if ready.status_code != 200 or ready.json()["active_sessions"]:
        raise RuntimeError("worker_not_idle_and_ready")
    print(json.dumps({"kind":"environment", "ready":ready.json()}), flush=True)
    for repetition, indices in [(0, range(len(batch)))] + [(i, sorted(set(round(j*(len(batch)-1)/29) for j in range(30)))) for i in range(1,4)]:
        for index in indices:
            case = batch[index]
            started = time.monotonic()
            try:
                response = client.post("/generate", json=case["payload"])
                row = {"kind":"measurement", "case":case["case"], "repetition":repetition,
                       "wall_seconds":time.monotonic()-started, "http_status":response.status_code}
                if response.status_code == 200:
                    row["result"] = response.json()
                    row["backend_id"] = response.headers.get("x-backend-id")
                else:
                    row["error"] = "worker_http_error"
            except httpx.HTTPError as exc:
                row = {"kind":"measurement", "case":case["case"], "repetition":repetition,
                       "wall_seconds":time.monotonic()-started, "http_status":None,
                       "error":type(exc).__name__}
            print(json.dumps(row, ensure_ascii=False), flush=True)
'''


def aggregate(rows):
    """All failures remain in the denominator; score an empty hypothesis for them."""
    counts = {key: sum(row["quality"][key] for row in rows) for key in
              ("reference_words", "substitutions", "deletions", "insertions", "correct")}
    errors = counts["substitutions"] + counts["deletions"] + counts["insertions"]
    good = [row for row in rows if row.get("http_status") == 200 and row.get("result", {}).get("text", "").strip()]
    seconds = sum(row.get("result", {}).get("audio_seconds", 0) for row in good)
    processing = sum(row.get("result", {}).get("processing_seconds", 0) for row in good)
    return {"requests": len(rows), "nonempty_successes": len(good), "failed_or_empty": len(rows)-len(good),
            **counts, "wer": errors / counts["reference_words"] if counts["reference_words"] else None,
            "decoded_audio_seconds": seconds, "summed_processing_seconds": processing,
            "summed_http_seconds": sum(row["wall_seconds"] for row in rows),
            "processing_real_time_factor": processing / seconds if seconds else None,
            "median_http_seconds": statistics.median(row["wall_seconds"] for row in rows) if rows else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin", "pod"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--parquet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.parquet.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == PARQUET_SHA256
    cases = pq.read_table(args.parquet).to_pylist()
    assert len(cases) == 1091 and all(words(case["text"]) for case in cases)
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    pod = json.loads(subprocess.check_output(kubectl + ["-n", "fs2-models", "get", "pod", args.pod, "-o", "json"]))
    assert pod["metadata"]["labels"].get("workload.fs2.nebius/owner") == "nemotron-speech-20260916"
    secret = json.loads(subprocess.check_output(kubectl + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    metadata = {
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "private HTTP worker quality/warm timing; NOT public gateway, cold-start or clinical acceptance",
        "dataset": "leduckhai/MultiMed", "config": "German", "split": "test", "revision": REVISION,
        "publisher_license_label": "MIT", "parquet_sha256": PARQUET_SHA256,
        "normalization": "NFKC/lowercase, punctuation ignored, XML tags removed; numbers, compounds, umlauts and hesitations unchanged",
        "corpus_rows": len(cases), "dataset_reported_audio_seconds": sum(case["duration"] for case in cases),
        "model": MODEL, "pod": args.pod, "pod_uid": pod["metadata"]["uid"],
        "image": next(c["image"] for c in pod["spec"]["containers"] if c["name"] == "speech"),
        "node": pod["spec"]["nodeName"], "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "driver_sha256": hashlib.sha256(DRIVER.encode()).hexdigest(),
        "warmup": "existing warmed snapshot-restored worker; full test pass precedes repeated warm subset",
        "repeat_subset": "30 evenly spaced row indices; three additional sequential repetitions; concurrency 1",
    }
    metadata_path = args.output.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    prefix = "acceptance/nemotron-speech-20260916/multimed-" + uuid4().hex + "/"
    objects, rows, s3, bucket = [], [], None, None
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False) as admin:
        try:
            login = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
            if login.status_code != 200:
                raise RuntimeError("admin_login_failed")
            users = admin.get("/admin/api/v1/users?tenant_id=rene").json()["data"]["items"]
            user = next(u for u in users if u["principal_id"] == "rene")
            response = admin.post(f"/admin/api/v1/users/{user['id']}/storage/credentials")
            if response.status_code != 200:
                raise RuntimeError("existing_test_storage_unavailable")
            credentials = response.json()["data"]
            bucket = credentials["bucket_name"]
            s3 = boto3.client("s3", endpoint_url=credentials["endpoint"], region_name=credentials["region"],
                              aws_access_key_id=credentials["access_key_id"], aws_secret_access_key=credentials["secret_access_key"],
                              config=Config(max_pool_connections=8))
            objects = [prefix + f"{index:04}.ogg" for index in range(len(cases))]

            def upload(index):
                raw = cases[index]["audio"]["bytes"]
                if not raw.startswith(b"OggS"):
                    raise ValueError("unexpected_audio_format")
                s3.put_object(Bucket=bucket, Key=objects[index], Body=raw, ContentType="audio/ogg")
                return {"case": index, "payload": {"audio": {
                    "url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": objects[index]}, ExpiresIn=7200),
                    "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "media_type": "audio/ogg"},
                    "options": {"model": MODEL, "language": "de-DE", "output_granularity": "word"}}}

            with ThreadPoolExecutor(max_workers=8) as pool:
                batch = list(pool.map(upload, range(len(cases))))
            print(json.dumps({"event": "inputs_staged", "clips": len(batch)}), flush=True)
            command = kubectl + ["-n", "fs2-models", "exec", "-i", args.pod, "-c", "speech", "--", "/opt/conda/bin/python", "-c", DRIVER]
            with tempfile.TemporaryFile() as stderr, args.output.open("x") as output:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
                try:
                    process.stdin.write(json.dumps(batch))
                    process.stdin.close()
                    for line in process.stdout:
                        row = json.loads(line)
                        if row["kind"] == "environment":
                            metadata["runtime"] = row["ready"]
                            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                            continue
                        case = cases[row["case"]]
                        row.update(reference=case["text"], input_path=case["audio"]["path"],
                                   input_sha256=batch[row["case"]]["payload"]["audio"]["sha256"],
                                   dataset_duration_seconds=case["duration"])
                        row["quality"] = alignment(case["text"], row.get("result", {}).get("text", ""))
                        rows.append(row)
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                        output.flush()
                        if len(rows) % 100 == 0 or len(rows) == 1091:
                            print(json.dumps({"event": "progress", **aggregate([r for r in rows if r["repetition"] == 0])}), flush=True)
                    if process.wait(timeout=30) != 0:
                        raise RuntimeError("benchmark_worker_failed; inspect task-owned worker logs")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=30)
            if len(rows) != 1181:
                raise RuntimeError("incomplete_benchmark")
        finally:
            cleanup_errors = []
            if s3 is not None:
                for start in range(0, len(objects), 1000):
                    result = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in objects[start:start+1000]]})
                    cleanup_errors.extend(result.get("Errors", []))
            metadata["temporary_audio_objects_deleted"] = not cleanup_errors
            metadata["finished_at"] = datetime.now(UTC).isoformat()
            metadata["received_measurements"] = len(rows)
            metadata["quality_full_test"] = aggregate([r for r in rows if r["repetition"] == 0])
            metadata["warm_repetitions"] = [aggregate([r for r in rows if r["repetition"] == i]) for i in range(1, 4)]
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
            admin.delete("/admin/api/v1/session")
    print(json.dumps({"event": "completed", **metadata["quality_full_test"]}), flush=True)


if __name__ == "__main__":
    main()
