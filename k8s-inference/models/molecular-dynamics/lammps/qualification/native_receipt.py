"""Compact digest-bound evidence; native acceptance never implies customer readiness."""

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from validate_case import validate


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load(path):
    return json.loads(path.read_text())


def case_receipt(directory, *, revalidate=False):
    workspace = directory / "workspace"
    qualification = load(directory / "qualification.json")
    validation_path = directory / "validation.json"
    if revalidate:
        try:
            validation = validate(workspace)
        except Exception as exc:
            validation = {"status": "failed", "error": str(exc)}
        validation_path = directory / "validation-final.json"
        validation_path.write_text(json.dumps(validation, indent=2) + "\n")
    validation = load(validation_path)
    result = load(workspace / "result.json")
    for entry in result["files"]:
        path = workspace / "data" / entry["path"]
        if path.stat().st_size != entry["size_bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("downloaded artifact differs from the native result inventory")
    execution = load(workspace / "execution.json")
    gpu = next(csv.DictReader((workspace / "environment.txt").read_text().splitlines(), skipinitialspace=True))
    gpu_name, driver = gpu["name"].strip(), gpu["driver_version"].strip()
    command_seconds = sum(c["wall_seconds"] for c in result["commands"])
    scientific = {key: value for key, value in validation.items() if key not in {"directory", "case", "repetition", "native_loops", "trajectories", "final_thermodynamics"}}
    initial_validation = directory / "validation.json"
    if initial_validation.exists():
        scientific["retained_initial_validation"] = {"path": str(initial_validation), "sha256": digest(initial_validation), "status": load(initial_validation)["status"], "final_revalidation_uses_native_interval_coverage": True}
    if qualification.get("collection_recovery"):
        scientific["artifact_collection_recovery"] = qualification["collection_recovery"]
    return {
        "case": qualification["job"], "status": validation["status"],
        "runtime_image": qualification["image"], "image_id": qualification["image_id"],
        "pool": "l40s-1x" if "L40S" in gpu_name else "h100-ondemand-1x",
        "gpu_name": gpu_name, "driver": driver,
        "gpu_and_driver": (workspace / "environment.txt").read_text().strip(),
        "pod_uid": qualification["pod_uid"], "node": qualification["node"],
        "input_sha256": digest(workspace / "input.tar.gz"),
        "input_manifest_sha256": digest(workspace / "fixture-manifest.json"),
        "request_sha256": digest(workspace / "request.json"),
        "result_sha256": digest(workspace / "result.json"),
        "validation_sha256": digest(validation_path), "raw_evidence": str(directory),
        "result_path": str(workspace / "result.json"), "validation_path": str(validation_path),
        "scientific_validation": scientific,
        "timing": {
            "native_commands_seconds": command_seconds,
            "worker_wall_seconds": execution["elapsed_seconds"],
            "worker_noncommand_overhead_seconds": execution["elapsed_seconds"] - command_seconds,
            "input_copy_seconds": qualification["input_copy_seconds"],
            "output_copy_seconds": qualification["output_copy_seconds"],
            "cpu_user_seconds": execution["cpu_user_seconds"],
            "cpu_system_seconds": execution["cpu_system_seconds"],
            "max_rss_kib": execution["max_rss_kib"],
            "all_data_inventory_bytes": sum(f["size_bytes"] for f in result["files"]),
            "output_io_native_seconds": validation.get("native_timing_seconds", {}).get("Output"),
            "timing_boundary": "local native worker; input/output copies use kubectl, not customer object storage; monitor included in CPU use",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", action="append", type=Path, required=True)
    parser.add_argument("--failures", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pool", action="append", choices=("h100-ondemand-1x", "l40s-1x"), help="Explicit completed subset; omitted requires both pools")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    tests, failures = [], []
    pools = args.pool or ["h100-ondemand-1x", "l40s-1x"]
    for campaign in args.campaign:
        for run in load(campaign / "campaign.json"):
            if run["repetition"] > args.repetitions:
                continue
            receipt = case_receipt(Path(run["directory"]), revalidate=True)
            if receipt["pool"] not in pools:
                continue
            receipt["repetition"] = run["repetition"]
            tests.append(receipt)
    for failure_root in args.failures:
        for path in sorted(failure_root.glob("*/qualification.json")):
            if load(path)["worker_exit"]:
                failures.append({"status": "failed", "raw_evidence": str(path.parent), "qualification_sha256": digest(path), "result_sha256": digest(path.parent / "workspace/result.json")})
    images = sorted({t["runtime_image"] for t in tests})
    if len(images) != 1:
        raise ValueError("a native runtime receipt must bind exactly one worker digest")
    cases = ("lj", "eam", "tersoff", "snap", "reaxff", "rhodo")
    required = {(pool, case, repetition) for pool in pools for case in cases for repetition in range(1, args.repetitions + 1)}
    full_campaign = {(pool, case, repetition) for pool in ("h100-ondemand-1x", "l40s-1x") for case in cases for repetition in (1, 2, 3)}
    passed = {(t["pool"], t["case"], t["repetition"]) for t in tests if t["status"] == "passed"}
    receipt = {"schema": "fs2-serve.nebius.ai/lammps-native-qualification/v1", "model_id": "lammps", "runtime_image": images[0], "recorded_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if required <= passed else "incomplete", "customer_ready": False, "customer_path_tested": False, "persistent_gpu_snapshot_tested": False, "tests": tests, "preserved_failures": failures, "limitations": ["Native KOKKOS single-GPU acceptance only; not hosted REST/MCP/customer-bucket qualification.", "Finite numerical/trajectory and broad NVE energy-span checks are not scientific convergence or potential suitability validation.", "Closed complete workspace/native binary restart is not a persistent GPU process snapshot.", "Wall-time segment boundaries may differ; chaotic trajectories are not required to remain bitwise identical."]}
    receipt["declared_cohort"] = {"pools": pools, "cases": list(cases), "repetitions_per_case": args.repetitions, "all_six_cases_required": True}
    receipt["campaign_incomplete"] = not full_campaign <= passed
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "tests": len(tests), "passed": len(passed), "runtime_image": images[0], "receipt_sha256": digest(args.output)}))


if __name__ == "__main__":
    main()
