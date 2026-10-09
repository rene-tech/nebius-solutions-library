"""Restore the recorded system/qa policy after a stopped, fully drained owner.

No submission, cancellation, resource deletion or customer-key access. This
exceptional cleanup keeps the original failed matrix and successful borrower
receipts intact. The normal supervisor still owns routine cleanup.
"""
import argparse
import asyncio
import base64
import os
from pathlib import Path

import httpx2

from run_rest import Campaign
from verify_concurrency import QA_KEY_ID, active_operations, load, now, save


def validate_policy(original, current):
    for value in (original, current):
        if (value.get("id") != QA_KEY_ID or value.get("tenant_id") != "system"
                or value.get("principal_id") != "qa" or value.get("expires_at") is not None):
            raise ValueError("Only the recorded non-expiring system/qa identity")
    if original.get("max_concurrency") != 2 or current.get("max_concurrency") not in (2, 3):
        raise ValueError("Policy changed outside the original two/three-slot benchmark")
    for field in ("models", "scopes", "expires_at", "tenant_id", "principal_id"):
        if original.get(field) != current.get(field):
            raise ValueError("Unrelated QA policy changed; do not overwrite it")


async def finalize(args):
    if (Path("/proc") / str(args.stopped_owner_pid)).exists():
        raise ValueError("Stop the original local owner before exceptional cleanup")
    if load(args.peer / "mpi-lane-progress.json", {}).get("all_verified") is not True:
        raise ValueError("The separate MPI lane has not completed")
    if load(args.output / "matrix-progress.json", {}).get("rest", {}).get("verified") is not True:
        raise ValueError("The original REST pair has not completed")
    original = load(args.output / "qa-policy-before.json")
    values = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_" + QA_KEY_ID.replace("-", "")[:12]):
        raise ValueError("Only the existing QA inference key")
    campaign = Campaign(args, None)
    secret = await campaign.kjson("-n", "fs2-system", "get", "secret", "fs2-serve-admin")
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    async with httpx2.AsyncClient(base_url=args.origin, timeout=30, trust_env=False,
                                 headers={"Authorization": "Bearer " + key}) as http, \
            httpx2.AsyncClient(base_url=args.origin, timeout=30, trust_env=False,
                              headers={"Origin": args.origin, "X-Requested-With": "XMLHttpRequest"}) as admin:
        if await active_operations(http):
            raise ValueError("QA operations remain active; cleanup cannot race another lane")
        response = await admin.post("/admin/api/v1/session", headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        try:
            current = await campaign.key_metadata(admin)
            validate_policy(original, current)
            response = await admin.patch("/admin/api/v1/keys/" + QA_KEY_ID, json={"max_concurrency": 2})
            response.raise_for_status()
            restored = await campaign.key_metadata(admin)
            validate_policy(original, restored)
            if restored["max_concurrency"] != 2:
                raise ValueError("QA policy did not return to its recorded baseline")
            save(args.output / "qa-policy-restored.json", restored)
            save(args.output / "exceptional-cleanup.json", {
                "at": now(), "stopped_owner_pid": args.stopped_owner_pid,
                "reason": "Local supervisor cleanup stalled after verified artifact collection",
                "original_failure_preserved": True, "active_qa_operations": 0,
                "max_concurrency": 2, "expires_at": None,
                "customer_state_changed": False, "gpu_resources_deleted": False,
            })
        finally:
            await admin.delete("/admin/api/v1/session")
    print("Restored existing system/qa concurrency to 2; no expiry or customer changes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "peer", "qa-env"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--stopped-owner-pid", type=int, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    args = parser.parse_args()
    if args.stopped_owner_pid <= 1:
        parser.error("Select the exact stopped local supervisor PID")
    os.umask(0o077)
    asyncio.run(finalize(args))


if __name__ == "__main__":
    main()
