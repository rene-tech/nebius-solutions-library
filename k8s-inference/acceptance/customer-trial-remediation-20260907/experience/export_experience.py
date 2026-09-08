"""Export one stopped sampler without hiding failures or phase-label lag."""

import argparse
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--scientific-start", required=True)
    boundary = parser.add_mutually_exclusive_group(required=True)
    boundary.add_argument("--scientific-end")
    boundary.add_argument("--scientific-client-stopped-at")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.raw / "sampler-completed.json").exists():
        raise ValueError("completed export requires stopped sampler receipt")
    source = (
        Path(__file__).parents[2] / "customer-trial-20260907/experience/summarize.py"
    )
    spec = importlib.util.spec_from_file_location("original_experience_summary", source)
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    rows = previous.read_lines(args.raw / "interactive.jsonl")
    if [row["ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("missing, repeated or reordered request ordinal")
    if any(row.get("submission_attempts") != 1 for row in rows):
        raise ValueError("unexpected client submission retry")
    start, end = (
        previous.timestamp(args.scientific_start),
        previous.timestamp(args.scientific_end or args.scientific_client_stopped_at),
    )
    fields = (
        "ordinal",
        "surface",
        "case",
        "phase",
        "started_at",
        "completed_at",
        "client_seconds",
        "request_sha256",
        "response_sha256",
        "status",
        "failure",
        "failure_detail",
        "failure_http_status",
        "correctness_passed",
        "operation_id",
        "submission_attempts",
        "status_polls",
        "mcp_calls",
        "mcp_http_statuses",
    )
    projected = []
    for row in rows:
        current = previous.timestamp(row["started_at"])
        projected.append(
            {
                **{key: row[key] for key in fields if key in row},
                "phase_by_start_timestamp": "baseline"
                if current < start
                else "during"
                if current <= end
                else "after",
                "operation_status": row.get("operation", {}).get("status"),
                "server_attempt": row.get("operation", {}).get("attempt"),
            }
        )
    result = {
        "schema": "fs2.customer-trial-remediation-experience/v1",
        "scientific_started_at": args.scientific_start,
        "scientific_completed_at": args.scientific_end,
        "scientific_client_stopped_at": args.scientific_client_stopped_at,
        "scientific_completion_confirmed": args.scientific_end is not None,
        "clock": "Public non-streaming request through complete validated output; "
        "includes session/network/admission/polling. Not TTFT or GPU decode time.",
        "total": previous.statistics_for(rows),
        "by_surface": {
            surface: previous.statistics_for(
                [row for row in rows if row["surface"] == surface]
            )
            for surface in ("http", "mcp")
        },
        "successful_response_latency": previous.statistics_for(
            [row for row in rows if row["status"] == "passed"]
        ),
        "by_actual_phase": {
            phase: previous.statistics_for(
                [row for row in projected if row["phase_by_start_timestamp"] == phase]
            )
            for phase in ("baseline", "during", "after")
        },
        "discovery": previous.read_lines(args.raw / "discovery.jsonl"),
        "requests": projected,
        "limitations": [
            "All attempts and failure latencies are retained. Success-only latency is explicitly separate; "
            "no client submission retries.",
            "Original phase labels are preserved; actual phase uses scientific start/end timestamps "
            "because file-based phase changes can lag.",
            "When scientific_client_stopped_at is set, after means after the bounded local-client stop; "
            "scientific server completion and clean resource recovery are not implied.",
            "No scientific correctness, production SLA or unseen burst coverage is inferred "
            "from this bounded synthetic workload.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result["total"]))


if __name__ == "__main__":
    main()
