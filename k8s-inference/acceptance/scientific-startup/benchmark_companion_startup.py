#!/usr/bin/env python3
"""Compare installed and candidate companion startup inside one bounded CPU pod.

The workload verifies the same deterministic 1 MiB artifact on every iteration.
This isolates process startup from model loading and is not an end-to-end model
cold-start benchmark. Run the fleet benchmark separately after deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--baseline-image", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 3:
        parser.error("at least three repetitions are required")
    rows = []
    content = b"fs2-scientific-startup\n" * 49932
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    with tempfile.TemporaryDirectory(prefix="fs2-companion-startup-") as directory:
        checkpoint = Path(directory) / "checkpoint.bin"
        checkpoint.write_bytes(content)
        marker = {
            "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
            "operation_id": "00000000-0000-4000-8000-000000000001",
            "attempt_id": "00000000-0000-4000-8000-000000000002",
            "tenant_id": "startup-benchmark",
            "model_id": "startup-benchmark",
            "variant_id": "startup-benchmark",
            "stage_id": "inference",
            "artifacts": [{
                "artifact_id": "checkpoint",
                "mount_path": str(checkpoint),
                "content_digest": digest,
                "artifact_manifest_sha256": "a" * 64,
                "localization_receipt_digest": digest,
                "sub_path": None,
                "readiness_receipt_sha256": "b" * 64,
                "authorization_receipt_sha256": "c" * 64,
                "verification_receipt": None,
                "files": [{"path": "checkpoint.bin", "digest": digest, "size_bytes": len(content)}],
                "aggregate_tree": None,
            }],
        }
        environment = {**os.environ, "FS2_RUNTIME_ARTIFACTS_JSON": json.dumps(marker)}
        environment.pop("PYTHONPATH", None)
        for repetition in range(1, args.repetitions + 1):
            for name in ("baseline", "candidate"):
                command = ["fs2-serve", "scientific-verify-runtime-artifacts"]
                run_environment = environment
                if name == "candidate":
                    command = [sys.executable, "-m", "fs2_serve.entrypoint", command[1]]
                    run_environment = {**environment, "PYTHONPATH": str(args.candidate_source)}
                started = time.monotonic()
                completed = subprocess.run(command, env=run_environment, capture_output=True, timeout=120)
                rows.append({
                    "variant": name,
                    "repetition": repetition,
                    "elapsed_seconds": time.monotonic() - started,
                    "returncode": completed.returncode,
                    "stderr": completed.stderr.decode(errors="replace")[:4096],
                })
    summary = {}
    for name in ("baseline", "candidate"):
        durations = [row["elapsed_seconds"] for row in rows if row["variant"] == name]
        summary[name] = {
            "median_seconds": statistics.median(durations),
            "min_seconds": min(durations),
            "max_seconds": max(durations),
        }
    receipt = {
        "schema": "fs2-serve.nebius.ai/companion-startup-benchmark/v1",
        "scope": "process startup plus deterministic artifact verification; no model inference",
        "baseline_image": args.baseline_image,
        "candidate_commit": args.candidate_commit,
        "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "input_digest": digest,
        "input_bytes": len(content),
        "cache_state": "same retained pod; alternating fresh processes; host page cache not flushed",
        "repetitions": args.repetitions,
        "runs": rows,
        "summary": summary,
        "status": "passed" if all(row["returncode"] == 0 for row in rows) else "failed",
    }
    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
