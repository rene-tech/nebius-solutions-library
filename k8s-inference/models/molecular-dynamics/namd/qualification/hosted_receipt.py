"""Bind downloaded-output audits to exact native results, without claiming API coverage."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from fs2_gromacs.files import digest_file
from fs2_namd.contracts import normalize


def receipt(request_path, materialized, validation_path, input_bundle, audit_name="artifact-audit.json"):
    request = normalize(json.loads(request_path.read_text()))
    validation = json.loads(validation_path.read_text())
    expected = [job["id"] for job in request["jobs"]]
    science = {row["job"]: row for row in validation["repetitions"]}
    if set(science) != set(expected) or len(validation["repetitions"]) != len(expected):
        raise ValueError("scientific validation does not cover the exact requested jobs")
    rows = []
    for job in expected:
        result_path = materialized / job / "result.json"
        audit_path = materialized / job / audit_name
        audit = json.loads(audit_path.read_text())
        if audit["job_id"] != job or audit["request_sha256"] != digest_file(request_path) or audit["result_sha256"] != digest_file(result_path):
            raise ValueError("artifact audit is not bound to the exact request/result bytes")
        bundle = audit.get("immutable_input_bundle")
        if bundle is not None and bundle["sha256"] != digest_file(input_bundle):
            raise ValueError("artifact audit used a different immutable input bundle")
        rows.append({"job_id": job,
                     "status": "passed" if audit["status"] == "passed" and science[job]["status"] == "succeeded" else "failed",
                     "result_path": str(result_path), "result_sha256": digest_file(result_path),
                     "artifact_audit_path": str(audit_path), "artifact_audit_sha256": digest_file(audit_path),
                     "verified_native_files": audit["verified_files"], "verified_native_bytes": audit["verified_bytes"],
                     "immutable_input_bundle": bundle,
                     "recipe_sha256": audit["recipe_sha256"], "scientific_validation": science[job]})
    return {"model_id": "namd", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
            "customer_ready": False, "scope": "scientific validation of downloaded hosted native outputs",
            "request_path": str(request_path), "request_sha256": digest_file(request_path),
            "input_path": str(input_bundle), "input_sha256": digest_file(input_bundle),
            "validation_path": str(validation_path), "validation_sha256": digest_file(validation_path),
            "tests": rows, "verified_native_files": sum(row["verified_native_files"] for row in rows),
            "verified_native_bytes": sum(row["verified_native_bytes"] for row in rows),
            "immutable_inputs_verified": all(row["immutable_input_bundle"] is not None for row in rows),
            "trajectory_frames": sum(info["frames"] for row in science.values() for info in row.get("trajectories", {}).values()),
            "single_trajectory_native_timing": {key: validation[key] for key in ("median_ns_per_day", "min_ns_per_day", "max_ns_per_day")},
            "limitations": ["API authentication, submitted input-bundle binding, materialization and runtime-image provenance belong to the linked parent client/release receipt",
                            "Native timing is not hosted end-to-end throughput or an MPS aggregate",
                            "No equilibrium, free-energy convergence or persistent GPU snapshot claim"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--materialized", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-name", default="artifact-audit.json", help="Retain earlier immutable receipts when selecting an additional audit")
    args = parser.parse_args()
    report = receipt(args.request, args.materialized, args.validation, args.input, args.audit_name)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.output), "sha256": digest_file(args.output),
                      "status": report["status"], "tests": len(report["tests"]),
                      "verified_native_files": report["verified_native_files"], "trajectory_frames": report["trajectory_frames"]}))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
