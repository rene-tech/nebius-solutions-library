#!/usr/bin/env python3
"""Export an explicitly incomplete cohort without inventing runner receipts.

Offline evidence only. Never sends requests, resumes operations, changes the
original raw files, or creates the missing aggregate/terminal receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ORIGINAL = Path(__file__).resolve().parents[2] / "customer-trial-20260907/workload"
sys.path.insert(0, str(ORIGINAL))
import export_results as original  # noqa: E402


def export(raw: Path, output: Path, stop: dict) -> dict:
    if (raw / "aggregate.json").exists():
        raise ValueError("Use the original exporter for a completed runner")
    plan = json.loads((raw / "campaign-plan.json").read_bytes())
    summaries = {
        value["id"]: value
        for path in raw.glob("*.customer-summary.json")
        for value in [json.loads(path.read_bytes())]
    }
    mixed_start = min(
        value["client_started_at"] for value in summaries.values()
        if value["phase"] == "mixed-batch"
    )
    output.mkdir(parents=True, exist_ok=False)
    write = original.trial.write_json
    write(output / "campaign-plan.json", plan)
    write(output / "bounded-stop.json", stop)
    rows, all_http, manifest = [], [], []
    for case in plan["scenarios"]:
        case_id = case["id"]
        summary = summaries.get(case_id, {})
        trace = [json.loads(line) for line in (raw / f"{case_id}.http.jsonl").read_text().splitlines()]
        all_http.extend(trace)
        receipt_path = raw / f"{case_id}.json"
        receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
        submits = [e for e in trace if e["method"] == "POST" and e["path"].endswith(":submit")]
        polls = [e for e in trace if e["method"] == "GET" and e["path"].startswith("/v1/operations/") and not e["path"].endswith("/result")]
        downloads = [e for e in trace if e["path"].startswith("/v1/artifacts/") and e["path"].endswith("/content")]
        latest = next((e for e in reversed(trace) if e.get("state", {}).get("operation", {}).get("id")), {})
        operation = latest.get("state", {}).get("operation", {})
        times = receipt.get("timestamps", operation)
        started = summary.get("client_started_at")
        if not started:
            started = stop["blocked_cases"][case_id]["client_started_at"]
        write(output / f"{case_id}.last-observed.json", latest)
        for suffix in (".json", ".submitted.json", ".customer-summary.json", ".http.jsonl"):
            path = raw / f"{case_id}{suffix}"
            if path.exists():
                content = path.read_bytes()
                manifest.append({"name": path.name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
                if suffix != ".http.jsonl":
                    write(output / path.name, json.loads(content))
        replay = (
            len(submits) == 2
            and submits[0].get("state", {}).get("operation", {}).get("id") == submits[1].get("state", {}).get("operation", {}).get("id")
            and submits[1].get("state", {}).get("operation", {}).get("reused") is True
        )
        rows.append({
            "id": case_id, "model_id": case["model_id"], "phase": case["phase"],
            "outcome": summary.get("outcome", "blocked_client_stopped"),
            "error_code": summary.get("error_code"),
            "original_runner_receipt_exists": bool(receipt),
            "operation_id_returned_to_client": summary.get("operation_id") or operation.get("id"),
            "execution_identity": receipt.get("execution_identity"),
            "timestamps": times,
            "client_wall_seconds": summary.get("wall_seconds"),
            "observed_client_interval_until_stop_seconds": original.elapsed(started, stop["stopped_at"]) if not summary else None,
            "client_slot_wait_seconds": original.elapsed(mixed_start, started) if case["phase"] == "mixed-batch" else None,
            "accepted_to_terminal_seconds": original.elapsed(times.get("accepted_at"), times.get("completed_at")),
            "accepted_to_result_seconds": original.elapsed(times.get("accepted_at"), times.get("result_completed_at")),
            "client_started_to_accepted_seconds": original.elapsed(started, times.get("accepted_at")),
            "resources_released": summary.get("resources_released"),
            "idempotent_replay_verified": replay,
            "hash_verified_download_count": len(summary.get("downloaded_artifacts", [])),
            "submit_response_seconds": [e["elapsed_seconds"] for e in submits],
            "download_response_seconds": [e["elapsed_seconds"] for e in downloads],
            "poll_count": len(polls),
            "poll_mean_response_seconds": sum(e["elapsed_seconds"] for e in polls) / len(polls) if polls else None,
            "poll_max_response_seconds": max((e["elapsed_seconds"] for e in polls), default=None),
            "http_failures": [e for e in trace if e.get("status", 0) >= 400 or e.get("error_code")],
            "stage_attempts": receipt.get("attempts", []),
            "last_observed_at": latest.get("at"),
            "last_observed_state": latest.get("state"),
        })
    result = {
        "schema": "fs2-serve.nebius.ai/customer-trial-interrupted-measurements/v1",
        "run_id": plan["run_id"], "source_commit": plan["source_commit"],
        "first_client_started_at": min(s["client_started_at"] for s in summaries.values()),
        "stopped_at": stop["stopped_at"], "runner_completed": False,
        "outcome_counts": dict(Counter(row["outcome"] for row in rows)),
        "http_status_counts": dict(Counter(str(e.get("status", e.get("error_code", "unknown"))) for e in all_http)),
        "same_operation_replays_verified": sum(row["idempotent_replay_verified"] for row in rows),
        "hash_verified_download_count": sum(row["hash_verified_download_count"] for row in rows),
        "all_task_resources_released": False,
        "rows": rows, "raw_artifact_manifest": manifest,
        "interpretation": [
            "Original successful and failed receipts are copied unchanged; no missing aggregate or RF terminal receipt is manufactured.",
            "A blocked client is not a terminal server failure. The final RF operation remains running and requires manager-owned cleanup or repair.",
            "The failed Proteina submit returned no operation ID. Its independently identified committed operation belongs in separate association evidence; the client outcome remains failed.",
            "Client-slot waiting precedes submission. Four client threads do not guarantee only four active server operations after an accepted-but-error response.",
            "Client wall includes uploads, replay, polling and downloads; these are not cold-start or kernel execution measurements.",
        ],
    }
    write(output / "partial-measurements.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.raw, args.output, json.loads(args.stop.read_bytes()))
    print(json.dumps({key: result[key] for key in ("run_id", "outcome_counts", "http_status_counts", "same_operation_replays_verified", "hash_verified_download_count")}))


if __name__ == "__main__":
    main()
