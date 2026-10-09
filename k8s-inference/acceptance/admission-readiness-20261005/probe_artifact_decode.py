#!/usr/bin/env python3
"""Local synthetic CPU probe; no network, credentials or production mutation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fs2_serve.scientific_artifacts import (
    ArtifactDirection,
    _artifacts_from_rows,
    artifact_storage_key,
)
from fs2_serve.scientific_cpu import run_scientific_cpu


def records(count):
    operation, attempt = uuid4(), uuid4()
    now = datetime.now(UTC)
    rows = []
    for index in range(count):
        digest = "sha256:" + hashlib.sha256(str(index).encode()).hexdigest()
        rows.append({
            "id": uuid4(), "attempt_id": attempt, "operation_id": operation,
            "tenant_id": "local-test", "stage_id": "workflow", "shard_id": "main",
            "direction": "output", "digest": digest, "size_bytes": index,
            "media_type": "application/octet-stream", "compression": None,
            "storage_key": artifact_storage_key(
                tenant_id="local-test", operation_id=operation, stage_id="workflow", shard_id="main",
                attempt_id=attempt, direction=ArtifactDirection.OUTPUT, digest=digest,
            ),
            "access_profile": "public", "access_receipt_digest": None,
            "retention_expires_at": now + timedelta(days=1), "created_at": now,
        })
    return rows


async def measure(rows, offload):
    lags = []
    stopped = False

    async def heartbeat():
        while not stopped:
            start = time.monotonic()
            await asyncio.sleep(0.005)
            lags.append(time.monotonic() - start - 0.005)

    async def decode():
        await asyncio.sleep(0)
        if offload:
            return await run_scientific_cpu(_artifacts_from_rows, rows)
        return _artifacts_from_rows(rows)

    probe = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.01)
    start = time.monotonic()
    decoded = await asyncio.gather(decode(), decode())
    elapsed = time.monotonic() - start
    await asyncio.sleep(0.01)
    stopped = True
    await probe
    assert [len(items) for items in decoded] == [len(rows)] * 2
    return {
        "offload": offload, "concurrent_inventories": 2, "records_per_inventory": len(rows),
        "elapsed_seconds": elapsed, "max_event_loop_lag_seconds": max(lags), "heartbeat_samples": len(lags),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=20007)
    parser.add_argument("--contended-two-cpus", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.records <= 32765:
        parser.error("records must be1..32765")
    contender = None
    if args.contended_two_cpus:
        available = sorted(os.sched_getaffinity(0))
        if len(available) < 2:
            parser.error("two available CPUs are required")
        os.sched_setaffinity(0, available[:2])
        # A single bounded, task-owned busy process competes on the same two
        # CPUs. This is affinity contention, not a production cgroup replica.
        contender = subprocess.Popen([  # noqa: S603 -- current interpreter and constant local-only program
            sys.executable, "-c", "import time\nend=time.monotonic()+20\nwhile time.monotonic()<end: pass",
        ])
    try:
        rows = records(args.records)
        results = [asyncio.run(measure(rows, offload)) for offload in (False, True)]
        print(json.dumps({"scope": "local synthetic metadata only", "python": sys.version.split()[0],
                          "contended_two_cpus": args.contended_two_cpus, "results": results}, indent=2))
    finally:
        if contender is not None:
            contender.terminate()
            contender.wait(timeout=5)


if __name__ == "__main__":
    main()
