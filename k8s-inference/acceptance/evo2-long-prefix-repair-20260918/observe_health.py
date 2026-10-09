"""Observe exact production health deadlines during the isolated HTTP replay."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--replay-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-requests", type=int, default=96)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    started = time.monotonic()
    next_ready, next_runtime = started, started
    events = []
    completed = False
    while time.monotonic() - started < 3600:
        for path, period in (("/v1/health/ready", 5), ("/v1/runtime", 30)):
            due = next_ready if period == 5 else next_runtime
            if time.monotonic() < due:
                continue
            before = time.monotonic()
            event = {"path": path, "started_unix_seconds": time.time(), "timeout_seconds": 3,
                     "production_period_seconds": period, "passed": False}
            try:
                with urllib.request.urlopen(args.base_url.rstrip("/") + path, timeout=3) as response:
                    body = json.loads(response.read())
                    event.update(status=response.status, passed=response.status == 200 and body.get("status") == "ready")
            except Exception as error:
                event["error"] = {"type": type(error).__name__, "detail": str(error)}
            event["wall_seconds"] = time.monotonic() - before
            events.append(event)
            if period == 5:
                next_ready = before + period
            else:
                next_runtime = before + period
        try:
            completed = json.loads(args.replay_summary.read_text())["requests"] >= args.expected_requests
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        report = {"observations": len(events), "passed": sum(event["passed"] for event in events),
                  "failed": sum(not event["passed"] for event in events), "replay_completed": completed,
                  "health_latency_max_seconds": max((event["wall_seconds"] for event in events), default=None),
                  "events": events}
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if completed:
            print(json.dumps({key: value for key, value in report.items() if key != "events"}), flush=True)
            break
        time.sleep(max(0.05, min(next_ready, next_runtime) - time.monotonic()))
    raise SystemExit(0 if completed and events and all(event["passed"] for event in events) else 1)


if __name__ == "__main__":
    main()
