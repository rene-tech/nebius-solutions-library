"""Read-only operator evidence for explicitly listed internal single-cell runs.

The operator credential is used only for reporting, never model invocation.
It remains in memory; no credentials or customer data are written to receipts.
"""

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path

import httpx

from qualify_api import checked, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--operation-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    secret = json.loads(
        subprocess.check_output(
            [
                "kubectl",
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
    origin = "https://89.169.99.188"
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, trust_env=False, timeout=120
    ) as client:
        checked(
            client.post(
                "/admin/api/v1/session", headers={"Authorization": "Bearer " + token}
            )
        )
        try:
            for operation_id in args.operation_id:
                detail = checked(
                    client.get(f"/admin/api/v1/scientific-runs/{operation_id}")
                )
                attribution = detail["data"]["run"]["attribution"]
                if (attribution["tenant_id"], attribution["principal_id"]) != (
                    "system",
                    "qa",
                ):
                    raise ValueError("Refusing to collect unrelated customer reporting")
                save(args.output / f"{operation_id}.json", detail)
                print(
                    json.dumps(
                        {
                            "operation_id": operation_id,
                            "operator_reporting": "available",
                        }
                    ),
                    flush=True,
                )
        finally:
            client.delete("/admin/api/v1/session")


if __name__ == "__main__":
    main()
