#!/usr/bin/env python3
"""Sample real public admin/discovery during a bounded rollout; no inference."""
import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("seconds must be between 1 and 1800")
    os.umask(0o077)
    access = json.loads(args.bundle.read_text())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    authorization = "Bearer " + access["credentials"]["inference_access_token"]
    deadline = time.monotonic() + args.seconds
    total = errors = 0
    async with httpx.AsyncClient(base_url=origin, timeout=15) as client:
        with args.output.open("x") as output:
            while time.monotonic() < deadline:
                for path in ("/admin/", "/v1/models"):
                    started = time.monotonic()
                    row = {"at": datetime.now(UTC).isoformat(), "path": path}
                    try:
                        response = await client.get(
                            path,
                            headers={"authorization": authorization} if path.startswith("/v1/") else {},
                        )
                        row["status"] = response.status_code
                        row["ok"] = response.status_code == 200
                        if path == "/v1/models" and row["ok"]:
                            row["model_count"] = len(response.json().get("data", []))
                    except (httpx.HTTPError, ValueError) as error:
                        row.update(ok=False, error_type=type(error).__name__)
                    row["seconds"] = round(time.monotonic() - started, 4)
                    total += 1
                    errors += not row["ok"]
                    output.write(json.dumps(row) + "\n")
                    output.flush()
                await asyncio.sleep(min(10, max(0, deadline - time.monotonic())))
    print(json.dumps({"samples": total, "failed_samples": errors, "receipt": str(args.output)}))


if __name__ == "__main__":
    asyncio.run(main())
