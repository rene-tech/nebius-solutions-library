"""Retain bounded, gap-free query windows and export safe Qwen route transitions.

This is read-only and invokes the existing private Loki capture helper. It never
submits inference, alters routes, or treats an empty log stream as an uptime SLA.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

QUERY = (
    '{k8s_namespace_name="fs2-system",k8s_container_name="control-plane"}'
    ' |= "model_publication_changed" |= "qwen3-8b"'
)
QUERY_LIMIT = 5000  # Same bound as the reused capture_logs.py helper.


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("window timestamps require an explicit timezone")
    return result.astimezone(UTC)


def windows(start: datetime, end: datetime):
    if not timedelta(0) < end - start <= timedelta(hours=4):
        raise ValueError("publication capture requires a positive window of at most four hours")
    current = start
    while current < end:
        following = min(current + timedelta(minutes=5), end)
        yield current, following
        current = following


def project_transitions(chunks: list[dict]) -> dict:
    events, seen, coverage = [], set(), []
    parse_errors = 0
    for chunk in chunks:
        payload = chunk.get("payload")
        streams = (payload or {}).get("response", {}).get("data", {}).get("result", [])
        line_count = sum(len(stream.get("values", [])) for stream in streams)
        success = (
            chunk.get("returncode") == 0
            and (payload or {}).get("response", {}).get("status") == "success"
            and (payload or {}).get("response", {}).get("data", {}).get("resultType") == "streams"
        )
        coverage.append({
            "start": chunk["start"], "end": chunk["end"],
            "returncode": chunk.get("returncode"), "line_count": line_count,
            "query_succeeded": success, "limit_reached": line_count >= QUERY_LIMIT,
            "sha256": chunk.get("sha256"),
        })
        for stream in streams:
            pod = stream.get("stream", {}).get("k8s_pod_name")
            pod_uid = stream.get("stream", {}).get("k8s_pod_uid")
            for stamp, line in stream.get("values", []):
                try:
                    event = json.loads("{" + line.partition("{")[2])
                    if event.get("event") != "model_publication_changed" or event.get("model_ref") != "qwen3-8b":
                        raise ValueError("unexpected event in filtered publication stream")
                    record = {
                        "timestamp_ns": int(stamp), "pod": pod, "pod_uid": pod_uid,
                        **{key: event.get(key) for key in (
                            "namespace", "name", "model_ref", "revision", "disposition",
                            "reason", "phase", "source_resource_version", "registry_projection_valid",
                        )},
                    }
                except (ValueError, TypeError, AttributeError):
                    parse_errors += 1
                    continue
                identity = json.dumps(record, sort_keys=True)
                if identity not in seen:
                    seen.add(identity)
                    events.append(record)
    events.sort(key=lambda row: (row["timestamp_ns"], row.get("pod_uid") or "", row.get("pod") or ""))
    withdrawals = []
    for index, event in enumerate(events):
        if event["disposition"] != "withdraw":
            continue
        following = next((
            row for row in events[index + 1:]
            if (row["pod_uid"], row["pod"]) == (event["pod_uid"], event["pod"])
            and row["disposition"] == "publish"
        ), None)
        withdrawals.append({
            "withdrawal": event,
            "next_publication": following,
            "duration_seconds": (following["timestamp_ns"] - event["timestamp_ns"]) / 1e9 if following else None,
            "open_at_window_end": following is None,
        })
    contiguous = bool(coverage) and all(
        timestamp(row["start"]) < timestamp(row["end"]) for row in coverage
    ) and all(
        timestamp(previous["end"]) == timestamp(following["start"])
        for previous, following in zip(coverage, coverage[1:], strict=False)
    )
    complete = contiguous and all(
        row["query_succeeded"] and not row["limit_reached"] for row in coverage
    ) and parse_errors == 0
    return {
        "schema": "fs2.customer-trial-publication-window/v1",
        "query": QUERY, "chunks": coverage, "query_windows_contiguous": contiguous,
        "query_evidence_complete": complete,
        "parse_errors": parse_errors, "events": events, "withdrawals": withdrawals,
        "verdict": "evidence-incomplete" if not complete else
        "withdrawal-observed" if withdrawals else "no-withdrawal-recorded",
        "limitations": [
            "Completeness covers the requested successful, untruncated Loki query windows, "
            "not logs lost before ingestion.",
            "Adjacent inclusive query boundaries are deduplicated by exact event and Pod identity.",
            "No-withdrawal-recorded does not prove uninterrupted network availability "
            "or replace client and readiness checks.",
            "Durations use retained Loki entry timestamps; an unmatched withdrawal remains open, never zero seconds.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Fresh private directory for raw query receipts")
    parser.add_argument("--report", type=Path, required=True, help="Fresh safe derived JSON report")
    args = parser.parse_args()
    bounds = list(windows(timestamp(args.start), timestamp(args.end)))
    if args.report.exists():
        parser.error("report already exists; preserve the completed capture and choose a fresh path")
    args.output.mkdir(parents=True, exist_ok=False)
    helper = Path(__file__).parents[2] / "customer-trial-20260907/observer/capture_logs.py"
    chunks = []
    for index, (start, end) in enumerate(bounds):
        raw_path = args.output / f"publication-{index:03d}.json"
        command = [
            sys.executable, str(helper), "--credential-bundle", str(args.credential_bundle),
            "--start", start.isoformat(), "--end", end.isoformat(), "--query", QUERY,
            "--output", str(raw_path),
        ]
        try:
            reply = subprocess.run(  # noqa: S603 - fixed helper and read-only query; no shell.
                command, capture_output=True, text=True, timeout=180, check=False,
            )
            code, diagnostic = reply.returncode, reply.stderr
        except subprocess.TimeoutExpired:
            code, diagnostic = -1, "bounded read-only log capture timed out"
        chunk = {"start": start.isoformat(), "end": end.isoformat(), "returncode": code}
        if raw_path.exists():
            raw = raw_path.read_bytes()
            chunk.update(payload=json.loads(raw), sha256=hashlib.sha256(raw).hexdigest())
        else:
            with (args.output / f"publication-{index:03d}-error.json").open("x") as stream:
                json.dump({**chunk, "diagnostic": diagnostic}, stream, indent=2)
        chunks.append(chunk)
    report = project_transitions(chunks)
    report.update(requested_start=args.start, requested_end=args.end)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({
        "chunks": len(chunks), "events": len(report["events"]),
        "withdrawals": len(report["withdrawals"]), "verdict": report["verdict"],
    }))
    if not report["query_evidence_complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
