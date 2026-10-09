"""Bind preserved upstream comparisons to a verified exact worker and GPU Pod.

Raw licensed inputs remain private. This compact receipt does not upgrade a
failed comparison, relax a tolerance, or replace the separate sustained cohort.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024**2), b""):
            value.update(block)
    return value.hexdigest()


def summarize(root, pod, source_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("exact runtime source commit is required")
    image = pod["spec"]["containers"][0]["image"]
    if "@sha256:" not in image:
        raise ValueError("upstream comparisons must be bound to a pinned worker")
    receipt_path = root / "regression.json"
    receipt = json.loads(receipt_path.read_text())
    paths = set()
    for item in receipt["files"]:
        path = root / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or item["path"] in paths:
            raise ValueError("invalid or duplicate preserved evidence path")
        paths.add(item["path"])
        if path.stat().st_size != item["size_bytes"] or digest(path) != item["sha256"]:
            raise ValueError("preserved regression evidence mismatch: " + item["path"])
    tests = []
    for item in receipt["tests"]:
        if item["log"] not in paths or digest(root / item["log"]) != item["log_sha256"]:
            raise ValueError("upstream comparison log was not preserved")
        compact = {key: item[key] for key in ("case", "precision", "status", "argv", "exit_code", "timed_out", "wall_seconds", "upstream_script_sha256", "log", "log_sha256")}
        compact["input_inventory_sha256"] = hashlib.sha256(json.dumps(item["source_files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        tests.append(compact)
    passed = sum(item["status"] == "passed" for item in tests)
    if len(tests) != 14 or len({(item["case"], item["precision"]) for item in tests}) != 14:
        raise ValueError("expected all fourteen distinct upstream comparisons")
    expected_status = "passed" if passed == 14 else "failed"
    if receipt["status"] != expected_status:
        raise ValueError("upstream cohort status does not preserve failed comparisons")
    gpu_name, gpu_uuid, driver, capability = [part.strip() for part in receipt["gpu"].split(",")]
    return {
        "schema": "fs2-serve.nebius.ai/amber-upstream-regression-summary/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "model_id": "amber", "runtime_image": image,
        "image_id": pod["status"]["containerStatuses"][0]["imageID"],
        "runtime_source_commit": source_commit,
        "engine_id": receipt["engine_id"], "pmemd_source_sha256": receipt["pmemd_source_sha256"],
        "status": receipt["status"], "passed_comparisons": passed, "total_comparisons": 14,
        "pool": "l40s-1x" if "L40S" in gpu_name else "h100-ondemand-1x",
        "gpu_name": gpu_name, "gpu_uuid": gpu_uuid, "driver": driver, "compute_capability": capability,
        "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
        "comparison_policy": receipt["comparison_policy"], "tests": tests,
        "raw_receipt_sha256": digest(receipt_path), "raw_evidence": str(root),
        "preserved_files_verified": len(paths), "customer_ready": False,
        "sustained_benchmark": False, "free_energy_convergence_claimed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--pod-json", type=Path, required=True)
    parser.add_argument("--runtime-source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("do not overwrite prior regression receipts")
    value = summarize(args.evidence, json.loads(args.pod_json.read_text()), args.runtime_source)
    value["pod_context_sha256"] = digest(args.pod_json)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({key: value[key] for key in ("status", "pool", "passed_comparisons", "total_comparisons", "preserved_files_verified")}))


if __name__ == "__main__":
    main()
