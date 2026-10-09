#!/usr/bin/env python3
"""Read-only recovery of receipts after a coordinator interrupts a rehearsal.

Never creates, retries, modifies or deletes workshop jobs. The original observer
summary is preserved; collection timing is not substituted for run latency.
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from rehearse_workshop import (
    AcceptanceFailure,
    Rehearsal,
    TERMINAL,
    read_credentials,
    validate_classification,
    validate_completed,
)


async def collect(args):
    source = json.loads(Path(args.summary).read_text())
    teams, denied = read_credentials(args.keys_file)
    config = SimpleNamespace(
        output=args.output,
        base_url=source["base_url"],
        insecure=args.insecure,
        ca_file=args.ca_file,
        run_label=source["label"] + "-readonly-collection",
        gateway_image=source["image_provenance"]["gateway"],
        workshop_image=source["image_provenance"]["workshop"],
    )
    client = Rehearsal(config, teams, denied)
    judge = next(item["judge"] for item in source["checks"] if item["name"] == "model_grants_and_fixed_judge")
    jobs = [
        (int(team), run_id)
        for repetition in source["repetitions"]
        for team, ids in repetition["run_ids"].items()
        for run_id in ids
    ]
    if not jobs:
        raise AcceptanceFailure("source summary contains no submitted jobs")
    started = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + args.timeout_seconds
    try:
        while True:
            rows = [(await client.request("GET", f"/v1/workshop/runs/{run_id}", team=team))[0] for team, run_id in jobs]
            print(
                json.dumps({"existing_jobs": len(jobs), "statuses": dict(Counter(row["status"] for row in rows))}),
                flush=True,
            )
            if all(row["status"] in TERMINAL for row in rows) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(10)
        reports, failures = [], []
        for (team, run_id), row in zip(jobs, rows, strict=True):
            report, _ = await client.request("GET", f"/v1/workshop/runs/{run_id}/report", team=team)
            events, status = await client.request(
                "GET", f"/v1/mindeval/runs/{run_id}/events", team=team, expected=(200, 404)
            )
            reports.append(
                {"team": teams[team]["label"], "report": report, "gateway_events": events if status == 200 else None}
            )
            try:
                if row["status"] != "completed":
                    raise AcceptanceFailure(f"{row['status']}: {row['state'].get('error')}")
                validate_completed(row, judge)
                validate_classification(row)
            except (AcceptanceFailure, KeyError, TypeError) as exc:
                failures.append({"run_id": run_id, "error": str(exc)})
        client.save("collected-reports.json", reports)
        result = {
            "schema": "fs2-mindeval-readonly-receipt-collection/v1",
            "source_summary": str(Path(args.summary)),
            "source_label": source["label"],
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "jobs": len(jobs),
            "strict_existing_run_validation_passed": not failures,
            "release_acceptance": False,
            "limitation": "Interrupted observer; this recovers evidence, not frozen-release or complete latency acceptance.",
            "http_methods": ["GET"],
            "failures": failures,
        }
        client.save("collection-summary.json", result)
        print(json.dumps(result), flush=True)
        return not failures
    finally:
        await client.client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--keys-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--ca-file")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(collect(args)) else 1)


if __name__ == "__main__":
    main()
