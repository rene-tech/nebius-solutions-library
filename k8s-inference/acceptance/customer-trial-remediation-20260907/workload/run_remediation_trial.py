#!/usr/bin/env python3
"""Rerun the exact original customer campaign in a fresh, uniquely named cohort.

No scientific client is reimplemented here. The existing runner owns submission,
idempotent replay, polling, semantic checks and hash-verified result downloads.
This wrapper checks fixture equivalence before delegation and never retries.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
ORIGINAL = ROOT / "acceptance/customer-trial-20260907/workload"
RUNNER = ORIGINAL / "run_customer_trial.py"
BASELINE = ORIGINAL / "results-r01/campaign-plan.json"
SCENARIO_FIELDS = (
    "id", "phase", "model_id", "fixture", "fixture_sha256", "input_sha256",
    "parameters", "service_class", "declared_inputs",
)


def verify_same_campaign(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    for field in (
        "runner_sha256", "scenarios_sha256", "model_counts",
        "max_parallel_clients", "operation_count",
    ):
        if baseline[field] != candidate[field]:
            raise ValueError(f"Original campaign changed: {field}")
    original_rows = baseline["scenarios"]
    candidate_rows = candidate["scenarios"]
    if len(original_rows) != len(candidate_rows):
        raise ValueError("Original campaign scenario count changed")
    for original, current in zip(original_rows, candidate_rows, strict=True):
        for field in SCENARIO_FIELDS:
            if original[field] != current[field]:
                raise ValueError(f"Original campaign changed: {original['id']}.{field}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--plan-output", type=Path)
    args = parser.parse_args()
    if args.cohort < 1:
        parser.error("--cohort must be a positive integer; failed cohorts are never overwritten")
    run_id = f"trial-customer-remediation-20260907-r{args.cohort:02d}"
    command = [
        sys.executable, str(RUNNER), "--access-bundle", str(args.access_bundle),
        "--output", str(args.output), "--run-id", run_id,
        "--timeout-seconds", str(args.timeout_seconds),
    ]
    prepared = subprocess.run(  # noqa: S603 -- fixed local runner, argument vector, no shell
        [*command, "--prepare-only"], check=True, capture_output=True, text=True,
    )
    plan = json.loads(prepared.stdout)
    verify_same_campaign(json.loads(BASELINE.read_bytes()), plan)
    if args.plan_output:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        with args.plan_output.open("x", encoding="utf-8") as stream:
            json.dump(plan, stream, sort_keys=True, indent=2)
            stream.write("\n")
    print(json.dumps({
        "event": "prepared", "run_id": run_id,
        "operation_count": plan["operation_count"],
        "max_parallel_clients": plan["max_parallel_clients"],
        "model_counts": plan["model_counts"],
        "baseline_fixture_equivalence": True,
        "source_commit": plan["source_commit"],
    }, sort_keys=True), flush=True)
    if args.prepare_only:
        return 0
    return subprocess.call(command)  # noqa: S603 -- same fixed local runner; preserve its exit status


if __name__ == "__main__":
    raise SystemExit(main())
