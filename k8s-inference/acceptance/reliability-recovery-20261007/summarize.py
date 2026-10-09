"""Publish only allowlisted recovery measurements, never private payloads/keys."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

MEASUREMENTS = (
    "source_operation",
    "resume_operation",
    "resumed_from_step",
    "checkpoint_step",
    "newly_completed_ns",
    "native_elapsed_seconds",
    "accepted_to_durable_seconds",
    "delivered_ns_per_day",
    "native_useful_ns_per_day",
    "native_segments",
    "zero_exit_segments",
    "verified_files",
    "history_files",
    "resume_protocol",
    "customer_key_used",
    "status",
)
CUSTOMER = (
    "at",
    "operation",
    "status",
    "generation",
    "checkpoint_step",
    "native_ns_per_day",
    "native_segments",
    "zero_exit_segments",
    "resume_from_step",
    "new_ns",
    "accepted_to_checkpoint_seconds",
    "delivered_ns_per_day",
    "verified_native_start_step",
    "original_scientific_files_retained",
    "native_performance_flags_verified",
    "customer_job_left_running",
    "minimum_delivered_ns_per_day",
    "performance_gate",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = {"recorded_at": datetime.now(UTC).isoformat(), "cohorts": {}}
    for name in ("cohort-a", "cohort-b", "cohort-c"):
        source = json.loads((args.private_evidence / name / "receipt.json").read_text())
        bucket = json.loads(
            (
                args.private_evidence / name / "independent-bucket-verification.json"
            ).read_text()
        )
        result = {key: source[key] for key in MEASUREMENTS if key in source}
        result["resume_protocol"] = source.get("resume_protocol", "rest")
        result["throughput_is_measurement_not_threshold_gate"] = True
        result["final_release_unchanged"] = name != "cohort-a"
        result["bucket_verification"] = {
            key: bucket[key]
            for key in (
                "status",
                "files",
                "unique_objects",
                "unique_bytes",
                "sha256_checked",
                "manifest_sha256",
            )
        }
        document["cohorts"][name] = result
    path = args.private_evidence / "customer/verification.json"
    if path.exists():
        source = json.loads(path.read_text())
        document["customer_recovery"] = {
            key: source[key] for key in CUSTOMER if key in source
        }
    else:
        document["customer_recovery"] = {"verification": "pending"}
    args.output.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
