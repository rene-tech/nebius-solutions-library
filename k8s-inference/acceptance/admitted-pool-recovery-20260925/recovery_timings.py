#!/usr/bin/env python3
"""Measure the declared 5-minute recovery SLO from retained public evidence."""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path

from run_acceptance import FAILURE, attempts, read, require, save, sha


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "unqualified_timestamp")
    return parsed


def measure(status, *, confirmation_seconds=120, target_seconds=300):
    require(status["operation"]["status"] == "succeeded", "successful_native_operation_required")
    rows = attempts(status)
    results = []
    for first in rows:
        if first.get("failure_code") != FAILURE:
            continue
        recovery = first["recovery"]
        require(recovery["state"] == "recovered" and first["resource_released"], "recovery_not_complete")
        following = [row for row in rows if row["stage_id"] == first["stage_id"]
                     and row["shard_id"] == first["shard_id"] and row["attempt_number"] == first["attempt_number"] + 1]
        require(len(following) == 1, "unique_replacement_required")
        second = following[0]
        require(second["outcome"] == "succeeded" and second["resource_released"], "replacement_not_complete")
        admitted = timestamp(first["scheduling_admission"]["admitted_at"])
        readmitted = timestamp(second["scheduling_admission"]["admitted_at"])
        # Live harness requires pool failure older than confirmation BEFORE
        # submission. Admission is therefore the later confirmation boundary.
        confirmed = admitted + timedelta(seconds=confirmation_seconds)
        detected = admitted + timedelta(seconds=recovery["admitted_wait_seconds"])
        require(admitted < confirmed <= detected <= readmitted, "recovery_timing_order_invalid")
        recovery_seconds = (readmitted - confirmed).total_seconds()
        require(recovery_seconds <= target_seconds, "recovery_slo_exceeded")
        results.append({
            "stage_id": first["stage_id"], "shard_id": first["shard_id"],
            "failed_attempt_id": first["attempt_id"], "replacement_attempt_id": second["attempt_id"],
            "original_admitted_at": admitted.isoformat(), "confirmed_at": confirmed.isoformat(),
            "detected_at": detected.isoformat(), "replacement_admitted_at": readmitted.isoformat(),
            "admission_to_detection_seconds": recovery["admitted_wait_seconds"],
            "admission_to_readmission_seconds": (readmitted - admitted).total_seconds(),
            "confirmed_to_readmission_seconds": recovery_seconds,
            "detected_to_readmission_seconds": (readmitted - detected).total_seconds(),
            "target_seconds": target_seconds, "target_met": True,
            "replacement_pool": second["scheduling_admission"]["resolved_pool_id"],
            "destination_model_loading_excluded": True,
        })
    require(results, "no_recovery_to_measure")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for directory in args.directories:
        receipt = read(directory / "receipt.json")
        require(receipt["state"] == "passed" and receipt["scenario"] == "recovery", "clean_cohort_required")
        results.append({"operation_id": receipt["operation_id"],
                        "receipt_sha256": sha(directory / "receipt.json"),
                        "terminal_sha256": sha(directory / "terminal.json"),
                        "measurements": measure(read(directory / "terminal.json"))})
    output = {"state": "measured", "source": "public retained attempt admission timestamps and recovery wait",
              "confirmation_seconds": 120, "target_seconds": 300, "cohorts": results}
    save(args.output, output)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
