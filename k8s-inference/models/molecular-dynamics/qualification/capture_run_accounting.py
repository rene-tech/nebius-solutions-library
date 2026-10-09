"""Read existing lifecycle facts for an explicit MD acceptance operation.

This does not change accounting, policies, workloads or budgets. Admin access is
used only to inspect the operator support surface; scientific acceptance still
comes from the ordinary-key customer invocation and its native artifacts.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

import httpx


def validate_subject(subject: dict, tenant: str, operation: str) -> str:
    if subject.get("tenant_id") != tenant or subject.get("operation_id") != operation:
        raise ValueError(
            "Lifecycle response does not match the explicitly requested owner/run."
        )
    return str(UUID(subject["subject_id"]))


def validate_detail(detail: dict, tenant: str, operation: str, subject_id: str) -> dict:
    if validate_subject(detail["subject"], tenant, operation) != subject_id:
        raise ValueError("Lifecycle detail changed the requested subject.")
    if detail.get("payloads_exposed") is not False:
        raise ValueError("Qualification receipt must not export customer payloads.")
    return detail


def checked(response):
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code}: {response.request.url.path}")
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--operation", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new receipt path; existing evidence is never replaced.")
    secret = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "--context",
                args.context,
                "-n",
                "fs2-system",
                "get",
                "secret",
                "fs2-serve-admin",
                "-o",
                "json",
            ]
        )
    )
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx.Client(
        base_url=args.origin,
        timeout=60,
        trust_env=False,
        headers={"origin": args.origin},
    ) as client:
        checked(
            client.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            )
        )
        try:
            listing = checked(
                client.get(
                    "/admin/api/v1/telemetry/workloads",
                    params={
                        "tenant_id": args.tenant,
                        "operation_id": str(args.operation),
                        "limit": 200,
                    },
                )
            )["data"]
            details = []
            for row in listing["items"]:
                subject_id = validate_subject(
                    row["subject"], args.tenant, str(args.operation)
                )
                detail = checked(
                    client.get("/admin/api/v1/telemetry/workloads/" + subject_id)
                )["data"]
                details.append(
                    validate_detail(
                        detail, args.tenant, str(args.operation), subject_id
                    )
                )
        finally:
            client.delete("/admin/api/v1/session")
    receipt = {
        "schema": "fs2-md-lifecycle-observation/v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": args.tenant,
        "operation_id": str(args.operation),
        "workloads": details,
        "changes_made": False,
        "interpretation": "Observed lifecycle facts, not a bill or inferred native performance.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
    ) as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "operation_id": str(args.operation),
                "workloads": len(details),
                "rollups": [item.get("rollup") for item in details],
            }
        )
    )


if __name__ == "__main__":
    main()
