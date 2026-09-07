#!/usr/bin/env python3
"""Export redacted customer receipts and derive explicit observation clocks."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import run_customer_trial as trial

MODEL_LABELS = {
    "proteina-complexa": "Proteina-Complexa", "boltzgen": "BoltzGen",
    "mosaic": "mosaic", "bindcraft": "BindCraft", "rfdiffusion": "RFdiffusion",
    "esmfold2": "ESMFold2", "esmfold2-fast": "ESMFold2-Fast",
    "protenix-v2": "Protenix v2", "alphafold3": "AlphaFold3",
}


def elapsed(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    return round((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds(), 6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    aggregate = json.loads((args.raw / "aggregate.json").read_bytes())
    plan = json.loads((args.raw / "campaign-plan.json").read_bytes())
    args.output.mkdir(parents=True, exist_ok=False)
    trial.write_json(args.output / "campaign-plan.json", plan)
    trial.write_json(args.output / "aggregate.json", aggregate)
    rows = []
    artifacts = []
    all_http = []
    mixed_phase_started = min(row["client_started_at"] for row in aggregate["rows"] if row["phase"] == "mixed-batch")
    for row in aggregate["rows"]:
        scenario_id = row["id"]
        trace_path = args.raw / f"{scenario_id}.http.jsonl"
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        all_http.extend(events)
        receipt_path = args.raw / f"{scenario_id}.json"
        if not receipt_path.exists():
            receipt_path = args.raw / f"{scenario_id}.wrapper-error.json"
        receipt = json.loads(receipt_path.read_bytes())
        trial.write_json(args.output / receipt_path.name, receipt)
        for path in (receipt_path, trace_path, args.raw / f"{scenario_id}.customer-summary.json"):
            raw = path.read_bytes()
            artifacts.append({"name": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
        submits = [event for event in events if event["method"] == "POST" and event["path"].endswith(":submit")]
        downloads = [event for event in events if event["method"] == "GET" and event["path"].startswith("/v1/artifacts/") and event["path"].endswith("/content")]
        polls = [event for event in events if event["method"] == "GET" and event["path"].startswith("/v1/operations/") and not event["path"].endswith("/result")]
        http_failures = [event for event in events if event.get("status", 0) >= 400 or event.get("error_code")]
        trace_states = []
        previous = None
        for event in events:
            state = event.get("state")
            if not state:
                continue
            signature = (state["operation"].get("status"), state["batch"].get("status"))
            if signature != previous:
                trace_states.append({"observed_at": event["at"], "operation": signature[0], "batch": signature[1]})
                previous = signature
        state_events = [event["state"]["operation"] for event in events if event.get("state", {}).get("operation", {}).get("id") == row.get("operation_id")]
        times = receipt.get("timestamps") or (state_events[-1] if state_events else {})
        terminal_attempts = [attempt for stage in receipt.get("terminal", {}).get("stages", []) for attempt in stage.get("attempts", [])]
        released = row.get("resources_released")
        if released is None and terminal_attempts:
            released = all(attempt.get("resource_released") is True for attempt in terminal_attempts)
        replay_verified = len(submits) == 2 and submits[0].get("state", {}).get("operation", {}).get("id") == submits[1].get("state", {}).get("operation", {}).get("id") and submits[1].get("state", {}).get("operation", {}).get("reused") is True
        gpu_attempts = [attempt for attempt in receipt.get("attempts", []) if (attempt.get("scheduling_admission") or {}).get("accelerator_count", 0) > 0]
        gpu_admissions = [attempt["scheduling_admission"]["admitted_at"] for attempt in gpu_attempts if attempt["scheduling_admission"].get("admitted_at")]
        first_admission = min(gpu_admissions, default=None)
        rows.append({
            "id": scenario_id, "model_id": row["model_id"], "phase": row["phase"],
            "requested_model_label": MODEL_LABELS[row["model_id"]],
            "execution_identity": receipt.get("execution_identity"),
            "operation_id": row.get("operation_id"), "outcome": row["outcome"],
            "error_code": row.get("error_code"),
            "client_wall_seconds": row.get("wall_seconds"),
            "client_slot_wait_from_mixed_phase_seconds": elapsed(mixed_phase_started, row["client_started_at"]) if row["phase"] == "mixed-batch" else None,
            "client_started_to_accepted_seconds": elapsed(row["client_started_at"], times.get("accepted_at")),
            "result_completed_to_client_finished_seconds": elapsed(times.get("result_completed_at"), row["client_finished_at"]),
            "accepted_to_result_seconds": elapsed(times.get("accepted_at"), times.get("result_completed_at")),
            "accepted_to_terminal_seconds": elapsed(times.get("accepted_at"), times.get("completed_at")),
            "accepted_to_operation_started_seconds": elapsed(times.get("accepted_at"), times.get("started_at")),
            "accepted_to_first_gpu_admitted_seconds": elapsed(times.get("accepted_at"), first_admission),
            "gpu_attempts": len(gpu_attempts),
            "stage_attempt_timing": [
                {
                    "stage_id": attempt["stage_id"], "attempt_id": attempt["attempt_id"],
                    "shard_id": attempt["shard_id"], "attempt_number": attempt["attempt_number"],
                    "status": attempt.get("status"),
                    "started_at": attempt.get("started_at"), "completed_at": attempt.get("completed_at"),
                    "elapsed_seconds": elapsed(attempt.get("started_at"), attempt.get("completed_at")),
                    "scheduling_admission": attempt.get("scheduling_admission"),
                    "pod_uids": attempt.get("pod_uids"), "gpu_uuids": attempt.get("gpu_uuids"),
                }
                for attempt in receipt.get("attempts", [])
            ],
            "attempts_over_one": sum(attempt.get("attempt_number", 1) > 1 for attempt in receipt.get("attempts", [])),
            "resources_released": released,
            "resource_release_source": "public successful receipt" if row.get("resources_released") is not None else "public failure terminal attempt state",
            "idempotent_replay": replay_verified,
            "submit_response_seconds": [round(event["elapsed_seconds"], 6) for event in submits],
            "download_response_seconds": [round(event["elapsed_seconds"], 6) for event in downloads],
            "poll_count": len(polls),
            "poll_mean_response_seconds": round(sum(event["elapsed_seconds"] for event in polls) / len(polls), 6) if polls else None,
            "poll_max_response_seconds": round(max(event["elapsed_seconds"] for event in polls), 6) if polls else None,
            "http_count": len(events), "http_failures": http_failures,
            "observed_state_transitions": trace_states,
            "downloaded_artifact_count": len(row.get("downloaded_artifacts", [])),
            "service_class": row.get("service_class"),
        })
    http_counts = Counter(str(event.get("status", event.get("error_code", "unknown"))) for event in all_http)
    summary = {
        "schema": "fs2-serve.nebius.ai/customer-trial-measurements/v1",
        "run_id": aggregate["run_id"], "started_at": aggregate["started_at"], "finished_at": aggregate["finished_at"],
        "mixed_phase_started_at": mixed_phase_started,
        "total_wall_seconds": elapsed(aggregate["started_at"], aggregate["finished_at"]),
        "passed": aggregate["passed"], "failed": aggregate["failed"],
        "peak_admitted_gpu_attempts": aggregate["peak_admitted_gpu_attempts"],
        "all_task_resources_released": all(row["resources_released"] is True for row in rows),
        "http_status_counts": dict(sorted(http_counts.items())),
        "clock_notes": [
            "Client wall includes fixture uploads, submit/replay, 5-second polling granularity and two result downloads.",
            "At most four operations are submitted concurrently; all14 are not queued on the server at once. RF four-shard inference is the actual server-side batch.",
            "Client-slot waiting is relative to the earliest mixed-phase worker start, before an operation is submitted; it must not be confused with server queue delay.",
            "Accepted-to-result is the authoritative public operation result interval, including queueing and execution.",
            "Operation started is orchestration start, not GPU readiness or first kernel.",
            "Kueue GPU admission timestamps have whole-second precision; subsecond negative differences are retained, not clamped.",
            "Admission reserves resources but does not prove Pod/GPU compute start. Root observation provides lifecycle phase evidence.",
            "Stage-attempt elapsed includes its lifecycle; it is not GPU active-compute time. Runtime identity is copied from validated result receipts.",
            "The original peak-admitted-attempt count includes successful result receipts only, excluding the cancelled unscheduled reservation; use observer evidence for total physical occupancy and queue reservations.",
            "One/two cases per model are customer observations, not stable percentiles, a production SLA, or fresh-node cold-start measurements.",
        ],
        "rows": rows,
        "raw_artifact_manifest": artifacts,
    }
    cleanup_path = args.raw / "bindcraft2-scheduler-blocked-cleanup.json"
    if cleanup_path.exists():
        cleanup = json.loads(cleanup_path.read_bytes())
        trial.write_json(args.output / cleanup_path.name, cleanup)
        summary["explicit_blocked_case_cleanup"] = cleanup
    trial.write_json(args.output / "measurements.json", summary)
    print(json.dumps({key: summary[key] for key in ("passed", "failed", "total_wall_seconds", "http_status_counts")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
