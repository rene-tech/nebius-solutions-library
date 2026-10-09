"""Inspect benchmark-owned operations; optionally cancel exact recorded orphans.

Cancellation only targets the dedicated benchmark tenant's invalidated trials,
never a customer operation. Evidence is retained before cancellation.
"""

import argparse
import json
import os
from pathlib import Path

import httpx

ORPHANED_PENDING = {"f3261842-a389-444c-a672-bcf1830e6e13", "5da2f0a1-c4ee-4bba-afb2-65abbc56a2cd"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cancel-recorded-orphans", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url="https://89.169.99.188", trust_env=False, timeout=90,
                      headers={"Authorization": "Bearer " + args.token_file.read_text().strip()}) as client:
        cursor, operations = None, []
        for _ in range(20):
            response = client.get("/v1/operations", params={"limit": 200, **({"cursor": cursor} if cursor else {})})
            response.raise_for_status()
            page = response.json()
            operations.extend(page["data"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        (args.directory / "operations.json").write_text(json.dumps(operations, indent=2))
        for operation in operations:
            if operation["protocol"] == "scientific-artifact-upload-v1":
                continue
            print(json.dumps({k: operation.get(k) for k in ("id", "model_id", "status", "accepted_at", "error_code", "error_detail")}))
            if args.cancel_recorded_orphans and operation["id"] in ORPHANED_PENDING:
                assert operation["tenant_id"] == "platform-benchmarks" and operation["model_id"] == "boltzgen"
                detail = client.get("/v1/operations/" + operation["id"])
                detail.raise_for_status()
                (args.directory / (operation["id"] + ".json")).write_text(json.dumps(detail.json(), indent=2))
                # Don't interrupt an orphan that has since progressed to compute.
                stages = detail.json()["batch"]["stages"]
                active = [a for s in stages for a in s["attempts"] if not a["resource_released"]]
                if not active or any(a["last_phase"] != "node_pending" for a in active):
                    raise RuntimeError("orphan_progressed_reinspect_before_cancelling")
                response = client.post("/v1/operations/" + operation["id"] + ":cancel")
                response.raise_for_status()
                print(json.dumps({"cancelled_owned_orphan": operation["id"]}))


if __name__ == "__main__":
    main()
