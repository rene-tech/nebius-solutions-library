"""Bind completed private scientific cohorts to one exact AMBER worker digest."""

import argparse
import csv
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

MD = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(MD / "amber/runtime"), str(MD / "gromacs/runtime")]
from fs2_amber import ENGINE_ID, PMEMD_SOURCE_SHA256
from fs2_amber.contracts import canonical, normalize
from fs2_gromacs.files import inventory

CASES = ("dhfr-nve", "myoglobin-gb8", "complex-ti-mbar-nearby")


def load(path):
    return json.loads(path.read_text())


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024**2), b""):
            value.update(block)
    return value.hexdigest()


def case_receipt(path):
    qualification = load(path / "qualification.json")
    root = path / "workspace"
    request = normalize(load(root / "request.json"))
    result = load(root / "result.json")
    validation_path = root / "validation-native-cadence-v2.json"
    if not validation_path.exists():
        validation_path = root / "validation.json"
    science = load(validation_path)
    job = next(job for job in request["jobs"] if job["id"] == qualification["case"])
    expected_recipe = hashlib.sha256(canonical({"request": request, "job": job["id"], "image": ENGINE_ID})).hexdigest()
    if result["recipe_sha256"] != expected_recipe or result["engine_id"] != ENGINE_ID or result["pmemd_source_sha256"] != PMEMD_SOURCE_SHA256:
        raise ValueError("result engine/source/native recipe does not match the exact local contract")
    if result["completed_steps"] != [step["id"] for step in job["steps"]] or result["status"] != "succeeded":
        raise ValueError("cannot issue a completed scientific receipt for an incomplete workflow")
    if inventory(root / "data", max_bytes=request["max_output_bytes"]) != result["files"]:
        raise ValueError("copied output differs from the result file inventory")
    gpu = next(csv.DictReader((root / "environment.txt").read_text().splitlines(), skipinitialspace=True))
    gpu_name, driver = gpu["name"].strip(), gpu["driver_version"].strip()
    execution = load(root / "execution.json")
    manifest = load(root / "fixture-manifest.json")
    if digest(root / "input.tar.gz") != manifest["input_sha256"] or digest(root / "request.json") != manifest["request_sha256"]:
        raise ValueError("frozen input or request differs from the original fixture manifest")
    passed = qualification["worker_exit"] == 0 and science["status"] == "passed" and execution["exit_code"] == 0
    return {"case": qualification["case"], "repetition": qualification["repetition"], "status": "passed" if passed else "failed", "pool": "l40s-1x" if "L40S" in gpu_name else "h100-ondemand-1x", "gpu_name": gpu_name, "driver": driver, "runtime_image": qualification["runtime_image"], "image_id": qualification["image_id"], "pod_uid": qualification["pod_uid"], "node": qualification["node"], "input_sha256": digest(root / "input.tar.gz"), "request_sha256": digest(root / "request.json"), "result_sha256": digest(root / "result.json"), "validation_sha256": digest(validation_path), "validation_path": str(validation_path), "original_qualification": {"status": qualification["status"], "sha256": digest(path / "qualification.json"), "original_validation_sha256": digest(root / "validation.json"), "unchanged_artifacts_revalidated": validation_path.name != "validation.json"}, "protocol": manifest["protocol"], "scientific_validation": science, "timing": {**execution, "input_copy_seconds": qualification["input_copy_seconds"], "output_copy_seconds": qualification["output_copy_seconds"], "artifact_inventory_bytes": sum(item["size_bytes"] for item in result["files"]), "boundary": "single native worker; input/output copies via kubectl, not hosted customer storage"}, "raw_evidence": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, action="append", required=True)
    parser.add_argument("--upstream", type=Path, action="append", default=[])
    parser.add_argument("--preserved-failure", type=Path, action="append", default=[])
    parser.add_argument("--pool", choices=("h100-ondemand-1x", "l40s-1x"), action="append", required=True)
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--case", action="append", choices=(*CASES, "alanine-opc-full"), help="Explicit complete declared cohort; omitted requires both classical cases and nearby-state TI")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("choose a new immutable receipt path")
    tests, excluded = [], []
    cases = args.case or CASES
    for campaign in args.campaign:
        for run in load(campaign / "campaign.json"):
            if run["case"] not in cases:
                path = Path(run["raw_evidence"])
                excluded.append({"case": run["case"], "repetition": run["repetition"], "status": run["status"], "qualification_sha256": digest(path / "qualification.json"), "result_sha256": digest(path / "workspace/result.json"), "raw_evidence": str(path), "reason": "separate explicitly excluded case, not relabeled as a passing member of the declared cohort"})
                continue
            if run["repetition"] <= args.repetitions:
                tests.append(case_receipt(Path(run["raw_evidence"])))
    images = {test["runtime_image"] for test in tests}
    if len(images) != 1 or any(test["image_id"].split("@")[-1] != test["runtime_image"].split("@")[-1] for test in tests):
        raise ValueError("scientific receipt must bind exactly one running worker digest")
    required = {(pool, case, repetition) for pool in args.pool for case in cases for repetition in range(1, args.repetitions + 1)}
    observed = {(test["pool"], test["case"], test["repetition"]) for test in tests}
    passed = {(test["pool"], test["case"], test["repetition"]) for test in tests if test["status"] == "passed"}
    upstream = [{"path": str(path), "sha256": digest(path), "status": load(path)["status"], "counts_as_sustained_scientific_acceptance": False} for path in args.upstream]
    failures = [{"path": str(path), "sha256": digest(path), "status": load(path).get("status", "see retained native evidence")} for path in args.preserved_failure]
    summary = []
    for pool in args.pool:
        for case in cases:
            values = [test["scientific_validation"]["ns_per_day"] for test in tests if test["pool"] == pool and test["case"] == case and test["status"] == "passed"]
            if values:
                summary.append({"pool": pool, "case": case, "repetitions": len(values), "ns_per_day_median": statistics.median(values), "ns_per_day_min": min(values), "ns_per_day_max": max(values), "matched_upstream_performance_claimed": False})
    receipt = {"schema": "fs2-serve.nebius.ai/amber-native-qualification/v1", "model_id": "amber", "recorded_at": datetime.now(timezone.utc).isoformat(), "runtime_image": next(iter(images)), "engine_id": ENGINE_ID, "pmemd_source_sha256": PMEMD_SOURCE_SHA256, "status": "passed" if required == observed == passed and len(tests) == len(required) else "incomplete", "declared_cohort": {"pools": args.pool, "cases": list(CASES), "repetitions_per_case": args.repetitions}, "tests": tests, "performance_summary": summary, "upstream_regressions": upstream, "preserved_failures": failures, "customer_ready": False, "customer_path_tested": False, "persistent_gpu_snapshot_tested": False, "limitations": ["Only the explicitly completed scientific cohort is accepted; upstream comparison failures remain failures at unchanged native tolerances.", "SPFP sodium TI NVE has an upstream printed-temperature tolerance exception; the sustained alchemical case explicitly uses DPFP.", "Preparation and native fresh-worker recovery are separate receipts, not inferred from production cohorts.", "Single-window TI/MBAR and bounded classical dynamics do not establish free-energy or molecular convergence.", "Closed-stage restart does not imply preservation of an exact stochastic random stream or a persistent CUDA process image."]}
    receipt["declared_cohort"]["cases"] = list(cases)
    receipt["excluded_case_evidence"] = excluded
    receipt["limitations"].append("Original eleven-state complex MBAR has a distant-lambda printed overflow; the separately named nearby-state control is not full-grid FEP acceptance.")
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "tests": len(tests), "sha256": digest(args.output), "runtime_image": receipt["runtime_image"]}))


if __name__ == "__main__":
    main()
