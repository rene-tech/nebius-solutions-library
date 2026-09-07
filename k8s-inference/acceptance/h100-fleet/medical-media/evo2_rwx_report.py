#!/usr/bin/env python3
"""Publish the separate verified shared-filesystem Evo2 handoff cohort."""
import argparse
import hashlib
import json
from pathlib import Path

from cache_copy import CHECKPOINT_SHA256
from summarize import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copy", required=True, type=Path)
    parser.add_argument("--trial", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pod = json.loads((args.copy / "pod-latest.json").read_text())
    logs = (args.copy / "copy.log").read_text()
    if pod["status"]["phase"] != "Succeeded" or "DESTINATION_HASH_VERIFIED" not in logs or "COPY_COMPLETE" not in logs:
        raise ValueError("Copy and full destination hash must have succeeded")
    row = summarize(args.trial)
    if row["semantic_status"] != "PASS":
        raise ValueError("Destination-cache model must pass original semantic checks")
    claims = {}
    for name in ("source", "destination"):
        pvc = json.loads((args.copy / (name + "-pvc.json")).read_text())
        claims[name] = {"claim": pvc["metadata"]["name"], "uid": pvc["metadata"]["uid"],
                        "pv": pvc["spec"]["volumeName"], "storage_class": pvc["spec"]["storageClassName"]}
    report = {"schema": "fs2-serve.nebius.ai/evo2-shared-cache-qualification/v1", "status": "PASS",
        "copy": {"pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
                 "checkpoint_bytes": 82253491694, "checkpoint_sha256": CHECKPOINT_SHA256,
                 "source_mounted_readonly": True, "claims": claims,
                 "source_clock_receipts": logs.splitlines(),
                 "copy_log_sha256": hashlib.sha256((args.copy / "copy.log").read_bytes()).hexdigest()},
        "destination_runtime_trial": row,
        "measurement_notes": ["One distinct shared-filesystem fresh-process cohort, not part of the prior three RWO cached-process median.",
            "Same image, checkpoint, H100/driver tuple, native kernels, precision and original Hopper semantic oracle.",
            "The destination was just copied and fully read for SHA256 on this same node; OS page cache was not evicted.",
            "Not a disk-cold or public activation measurement. Source RWO checkpoint and CPU mount holder are retained.",
            "Root may now perform production bootstrap and independent public HTTP/MCP verification."]}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "container_to_ready_seconds": row["container_start_to_application_ready_seconds"],
                      "pod_to_ready_seconds": row["pod_creation_to_application_ready_seconds"], "phases": row.get("startup_phase_seconds")}))


if __name__ == "__main__":
    main()
