#!/usr/bin/env python3
"""One approved test cleanup, not an automatic workaround or model retry."""

import argparse
import json
import time
from pathlib import Path

import run_customer_trial as trial

EXPECTED_OPERATION = "2ec36323-564e-4a58-9091-9c55e6e7d064"
EXPECTED_JOB = "fs2-design-design-000-a1-dc7900545d35"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--observer-evidence", required=True)
    args = parser.parse_args()
    submitted = json.loads((args.run_root / "batch-06-bindcraft.submitted.json").read_bytes())
    assert submitted["operation_id"] == EXPECTED_OPERATION
    access = json.loads(args.access_bundle.read_bytes())
    client = trial.public.PublicApiClient(
        access["endpoints"]["inference_base_url"].removesuffix("/v1"),
        access["credentials"]["scientific_access_token"],
    )
    path = f"/v1/operations/{EXPECTED_OPERATION}"
    before = trial.public._json_response(client.request("GET", path), 200, "cleanup_status")
    assert before["operation"]["id"] == EXPECTED_OPERATION
    assert before["operation"]["model_id"] == "bindcraft"
    assert any(attempt.get("workload_name") == EXPECTED_JOB for stage in before["batch"]["stages"] for attempt in stage["attempts"])
    assert before["batch"]["status"] not in trial.public.TERMINAL
    started_at = trial.utc_now()
    response = client.request("DELETE", path)
    record = {
        "schema": "fs2-serve.nebius.ai/customer-trial-explicit-cleanup/v1",
        "operation_id": EXPECTED_OPERATION, "scenario_id": "batch-06-bindcraft",
        "outcome": "failed_scheduler_blocked",
        "reason": "BindCraft stage requests 16 CPU plus a 0.1 CPU collector and cannot fit selected h100-1x node allocatable CPU; FailedScheduling Insufficient cpu and NotTriggerScaleUp retained by observer. Not ordinary elastic capacity waiting.",
        "approved_action": "Explicitly cancel only the blocked test-owned operation. No retry, capacity, queue or policy changes.",
        "observer_evidence": args.observer_evidence,
        "before": trial.projection(before), "delete_at": started_at, "delete_status": response.status,
    }
    trial.write_json(args.run_root / "bindcraft2-scheduler-blocked-cancel-issued.json", record)
    trial.public._json_response(response, 202, "cancel")
    deadline = time.monotonic() + 180
    while True:
        status = trial.public._json_response(client.request("GET", path), 200, "cleanup_status")
        attempts = [attempt for stage in status["batch"]["stages"] for attempt in stage["attempts"]]
        if status["batch"]["status"] in trial.public.TERMINAL and all(attempt["resource_released"] for attempt in attempts):
            record.update(finished_at=trial.utc_now(), after=trial.projection(status), resources_released=True)
            trial.write_json(args.run_root / "bindcraft2-scheduler-blocked-cleanup.json", record)
            print(json.dumps({"operation_id": EXPECTED_OPERATION, "outcome": record["outcome"], "terminal": status["batch"]["status"], "resources_released": True, "delete_at": started_at}))
            return 0
        if time.monotonic() >= deadline:
            record.update(finished_at=trial.utc_now(), after=trial.projection(status), resources_released=False)
            trial.write_json(args.run_root / "bindcraft2-scheduler-blocked-cleanup.json", record)
            print(json.dumps({"operation_id": EXPECTED_OPERATION, "outcome": record["outcome"], "cleanup_timeout": True}))
            return 1
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
