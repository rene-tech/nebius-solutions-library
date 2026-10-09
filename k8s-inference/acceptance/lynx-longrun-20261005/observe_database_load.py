"""Bounded, read-only database-load evidence without SQL or customer payloads.

Query classes are classified inside PostgreSQL. Parallel workers are recorded
separately from client backends, so worker counts cannot masquerade as requests.
Uses the existing operator diagnostic path; never extracts database credentials.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
SQL = """
SELECT json_build_object('observed_at',clock_timestamp(),'activity',
 coalesce(json_agg(row_to_json(s)), '[]'::json)) FROM (
 SELECT backend_type,leader_pid IS NOT NULL AS parallel_worker,
   application_name,wait_event_type,wait_event,
   CASE
    WHEN query LIKE '%WITH candidate AS%' AND query LIKE '%fs2_scientific_batches%'
      THEN 'scientific_claim'
    WHEN query LIKE '%fs2_request_telemetry%' THEN 'request_telemetry'
    WHEN query LIKE '%fs2_gpu_lifecycle%' THEN 'gpu_lifecycle'
    WHEN query LIKE '%fs2_scientific_batches%' THEN 'scientific_state_other'
    WHEN query LIKE '%fs2_operations%' THEN 'operations_other'
    ELSE 'other'
   END AS query_class,
   md5(query) AS query_fingerprint,count(*) AS processes,
   round(max(extract(epoch FROM clock_timestamp()-query_start))::numeric,3)
      AS oldest_query_seconds
 FROM pg_stat_activity
 WHERE pid<>pg_backend_pid() AND state='active'
 GROUP BY backend_type,leader_pid IS NOT NULL,application_name,
   wait_event_type,wait_event,query_class,md5(query)
) s;
"""


def command(arguments):
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, "--request-timeout=10s", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        # Do not retain unbounded stderr or accidentally capture secret values.
        return {"status": "unavailable", "exit_code": result.returncode}
    return {"status": "observed", "data": result.stdout}


def capture():
    database = command(
        [
            "-n",
            "fs2-data",
            "exec",
            "fs2-control-db-1",
            "-c",
            "postgres",
            "--",
            "psql",
            "-X",
            "-At",
            "-c",
            SQL,
        ]
    )
    if database["status"] == "observed":
        database["data"] = json.loads(database["data"])
    cpu = command(["-n", "fs2-data", "top", "pod", "fs2-control-db-1", "--no-headers"])
    if cpu["status"] == "observed":
        fields = cpu.pop("data").split()
        cpu.update(zip(("pod", "cpu", "memory"), fields, strict=True))
    return {"at": datetime.now(UTC).isoformat(), "database": database, "resources": cpu}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600 or not 2 <= args.interval <= 30:
        parser.error("Use 10..600 seconds and 2..30 second sample interval")
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with args.output.open("x") as stream:
        count = 0
        while time.monotonic() - started < args.seconds:
            tick = time.monotonic()
            try:
                item = capture()
            except (subprocess.TimeoutExpired, ValueError) as error:
                item = {
                    "at": datetime.now(UTC).isoformat(),
                    "status": "unavailable",
                    "error_type": type(error).__name__,
                }
            stream.write(json.dumps(item) + "\n")
            stream.flush()
            count += 1
            if count == 1:
                print(
                    json.dumps({"status": "armed", "output": str(args.output)}),
                    flush=True,
                )
            time.sleep(max(0, args.interval - (time.monotonic() - tick)))
    print(json.dumps({"status": "finished", "samples": count}), flush=True)


if __name__ == "__main__":
    main()
