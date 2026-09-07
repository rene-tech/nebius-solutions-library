#!/usr/bin/env python3
"""Bounded public-API customer simulation; no administrative mutations.

Reuse the model-owned fixture, submission, replay, semantic validation and
artifact-download checks from scientific-fleet/run_scenario_acceptance.py.
The only wrapper additions are a sequential prefix, bounded mixed fan-out,
and per-request/per-operation state traces. Credentials remain in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance/scientific-fleet"))
import run_acceptance as public  # noqa: E402
import run_fleet_acceptance as fleet  # noqa: E402
import run_scenario_acceptance as scenario_client  # noqa: E402

CONTEXT = threading.local()
BASE_CLIENT = public.PublicApiClient
BASE_PREPARE = scenario_client.prepare_scenario


def prepare_trial_scenario(config: public.RunConfig, scenario: dict[str, Any]):
    """Change customer metadata only, retaining accepted scientific inputs."""
    model_id, request, declarations, fragment = BASE_PREPARE(config, scenario)
    campaign = config.run_id.removesuffix(f".{scenario['id']}")
    request["client_context"] = {
        "batch_id": f"{campaign}.{scenario['phase']}",
        "correlation_id": config.run_id,
        "display_name": f"Trial customer | {scenario['phase']} | {scenario['id']}",
    }
    return model_id, request, declarations, fragment


scenario_client.prepare_scenario = prepare_trial_scenario


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: dict[str, Any]) -> None:
    public._assert_redacted(value)
    public._write_receipt(path, value, overwrite=False)


def projection(document: dict[str, Any]) -> dict[str, Any]:
    """Project status only; never retain request bodies or response headers."""
    operation = document.get("operation") or {}
    batch = document.get("batch") or {}
    return {
        "operation": {
            key: operation.get(key)
            for key in (
                "id", "model_id", "status", "reused", "accepted_at", "started_at",
                "completed_at", "semantic_outcome", "failure_code", "runtime",
            )
        },
        "batch": {
            key: batch.get(key)
            for key in ("batch_id", "status", "failure_code", "result_published")
        },
        "stages": [
            {
                "stage_id": stage.get("stage_id"),
                "status": stage.get("status"),
                "failure_code": stage.get("failure_code"),
                "attempts": [
                    {
                        key: attempt.get(key)
                        for key in (
                            "attempt_id", "shard_id", "attempt_number", "outcome",
                            "last_phase", "resource_released", "failure_kind",
                            "failure_code", "scheduling_admission", "workload_name",
                            "workload_uid", "workload_namespace",
                        )
                    }
                    for attempt in stage.get("attempts", [])
                ],
            }
            for stage in batch.get("stages", [])
        ],
    }


class TracingClient(BASE_CLIENT):
    """Observe the unchanged public client without retries or auth logging."""

    def request(self, method: str, path: str, **kwargs: Any) -> public.HttpResponse:
        before = time.monotonic()
        event: dict[str, Any] = {
            "at": utc_now(), "method": method, "path": path,
        }
        try:
            response = super().request(method, path, **kwargs)
        except public.AcceptanceError as error:
            event.update(error_code=error.code, elapsed_seconds=time.monotonic() - before)
            self._trace(event)
            raise
        event.update(status=response.status, elapsed_seconds=time.monotonic() - before)
        if path.endswith(":submit") or (
            path.startswith("/v1/operations/") and not path.endswith("/result")
        ):
            try:
                document = json.loads(response.body)
                if isinstance(document, dict):
                    event["state"] = projection(document)
            except ValueError:
                event["invalid_json"] = True
        self._trace(event)
        return response

    @staticmethod
    def _trace(event: dict[str, Any]) -> None:
        public._assert_redacted(event)
        with CONTEXT.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")


def resource_release(receipt: dict[str, Any]) -> bool:
    attempts = [
        attempt
        for stage in receipt["queue"]["observed_stages"]
        for attempt in stage["attempts"]
    ]
    return bool(attempts) and all(item["resource_released"] is True for item in attempts)


def run_one(
    scenario: dict[str, Any], *, endpoint: str, bearer: str,
    fragment: Path, run_root: Path, run_id: str, timeout: float,
) -> dict[str, Any]:
    CONTEXT.trace_path = run_root / f"{scenario['id']}.http.jsonl"
    started_at = utc_now()
    print(json.dumps({"event": "started", "id": scenario["id"], "at": started_at}), flush=True)
    config = public.RunConfig(
        endpoint=endpoint, repository_root=ROOT, activation_fragment=fragment,
        receipt_path=run_root / f"{scenario['id']}.json",
        run_id=f"{run_id}.{scenario['id']}", timeout_seconds=timeout,
    )
    try:
        row = scenario_client.run_scenario(config, scenario, bearer)
    except Exception as error:
        # Preserve a wrapper defect without exposing transport text or silently
        # resubmitting a possibly accepted request.
        row = {
            "id": scenario["id"], "model_id": scenario["model_id"],
            "outcome": "failed", "error_code": "customer_wrapper_exception",
            "exception_type": type(error).__name__,
        }
        write_json(run_root / f"{scenario['id']}.wrapper-error.json", row)
    row.update(phase=scenario["phase"], client_started_at=started_at, client_finished_at=utc_now())
    if row["outcome"] == "passed" and config.receipt_path.exists():
        receipt = json.loads(config.receipt_path.read_bytes())
        row["resources_released"] = resource_release(receipt)
        row["timestamps"] = receipt["timestamps"]
        row["semantic_validation"] = receipt["terminal_state"]["semantic_validation"]
    write_json(run_root / f"{scenario['id']}.customer-summary.json", row)
    print(json.dumps({key: row.get(key) for key in ("id", "outcome", "error_code", "wall_seconds", "resources_released", "operation_id")}), flush=True)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="trial-customer-20260907-r01")
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    scenarios_path = Path(__file__).with_name("scenarios.json")
    scenarios_bytes = scenarios_path.read_bytes()
    scenarios = json.loads(scenarios_bytes)
    fragments = {item.model_id: item for item in fleet.discover_inputs(ROOT)}
    assert len(scenarios) == 14
    assert len({item["id"] for item in scenarios}) == 14
    counts = {model: sum(row["model_id"] == model for row in scenarios) for model in sorted({row["model_id"] for row in scenarios})}
    validated = []
    for scenario in scenarios:
        fragment = fragments[scenario["model_id"]]
        config = public.RunConfig(
            endpoint="https://example.invalid", repository_root=ROOT,
            activation_fragment=fragment.path, receipt_path=Path("unused"),
            run_id=f"{args.run_id}.{scenario['id']}",
        )
        _model, request, declarations, _fragment = scenario_client.prepare_scenario(config, scenario)
        validated.append({
            "id": scenario["id"], "phase": scenario["phase"],
            "model_id": scenario["model_id"], "fixture": fragment.relative_path,
            "fixture_sha256": fragment.sha256,
            "request_sha256": hashlib.sha256(public._canonical_json(request)).hexdigest(),
            "input_sha256": request["input_manifest"]["sha256"],
            "service_class": request["service_class"], "parameters": request["parameters"],
            "client_context": request["client_context"],
            "declared_inputs": len(declarations),
        })
    plan = {
        "schema": "fs2-serve.nebius.ai/customer-trial-plan/v1",
        "run_id": args.run_id, "created_at": utc_now(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scenarios_sha256": hashlib.sha256(scenarios_bytes).hexdigest(),
        "model_counts": counts, "max_parallel_clients": 4,
        "operation_count": len(scenarios), "scenarios": validated,
        "mutations": "Public input uploads and scientific submissions only; no administrative or infrastructure changes.",
        "scope": "Synthetic platform acceptance; no scientific or clinical validity claim, no cold-node or load-capacity benchmark.",
    }
    if args.prepare_only:
        print(json.dumps(plan, sort_keys=True, indent=2))
        return 0
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(args.output / "campaign-plan.json", plan)
    access = json.loads(args.access_bundle.read_bytes())
    endpoint = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    bearer = access["credentials"]["scientific_access_token"]
    public.PublicApiClient = TracingClient
    rows = []
    started_at = utc_now()

    def execute(item: dict[str, Any]) -> dict[str, Any]:
        return run_one(item, endpoint=endpoint, bearer=bearer, fragment=fragments[item["model_id"]].path, run_root=args.output, run_id=args.run_id, timeout=args.timeout_seconds)

    for item in scenarios:
        if item["phase"] == "sequential-switch":
            rows.append(execute(item))
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(execute, item) for item in scenarios if item["phase"] == "mixed-batch"]
        for future in as_completed(futures):
            rows.append(future.result())
    summary = {
        "schema": "fs2-serve.nebius.ai/customer-trial-outcome/v1",
        "run_id": args.run_id, "started_at": started_at, "finished_at": utc_now(),
        "source_commit": plan["source_commit"], "scenario_sha256": plan["scenarios_sha256"],
        "passed": sum(row["outcome"] == "passed" for row in rows),
        "failed": sum(row["outcome"] != "passed" for row in rows),
        "all_success_resources_released": all(row.get("resources_released", False) for row in rows if row["outcome"] == "passed"),
        "peak_admitted_gpu_attempts": scenario_client.gpu_overlap(rows),
        "rows": sorted(rows, key=lambda row: row["id"]),
    }
    write_json(args.output / "aggregate.json", summary)
    print(json.dumps({key: summary[key] for key in ("passed", "failed", "all_success_resources_released", "peak_admitted_gpu_attempts", "finished_at")}), flush=True)
    return int(summary["failed"] > 0 or not summary["all_success_resources_released"])


if __name__ == "__main__":
    sys.exit(main())
