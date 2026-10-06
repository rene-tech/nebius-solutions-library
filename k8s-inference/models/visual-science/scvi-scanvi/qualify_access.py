"""Add only scVI to the existing system/qa key; never create or rotate keys."""

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import httpx

from qualify_api import checked, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    values = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    token_id = str(UUID(hex=key.split("_")[2]))
    origin = "https://89.169.99.188"
    with httpx.Client(base_url=origin, trust_env=False, timeout=60) as customer:
        me = checked(customer.get("/v1/me", headers={"Authorization": "Bearer " + key}))
    if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
        raise ValueError("Expected the existing system/qa identity")
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
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, trust_env=False, timeout=60
    ) as client:
        checked(
            client.post(
                "/admin/api/v1/session", headers={"Authorization": "Bearer " + token}
            )
        )
        try:
            items = checked(
                client.get("/admin/api/v1/keys", params={"tenant_id": "system"})
            )["data"]["items"]
            before = next(item for item in items if item["id"] == token_id)
            if (before["tenant_id"], before["principal_id"], before["revoked_at"]) != (
                "system",
                "qa",
                None,
            ):
                raise ValueError("QA identity changed")
            desired = sorted(set(before["models"]) | {"scvi-scanvi"})
            after = before
            if args.apply and desired != sorted(before["models"]):
                after = checked(
                    client.patch(
                        f"/admin/api/v1/keys/{token_id}", json={"models": desired}
                    )
                )["data"]
            retained = (
                "scopes",
                "max_concurrency",
                "expires_at",
                "request_budget",
                "gpu_seconds_budget",
                "rate_limit_requests",
                "rate_window_seconds",
            )
            if any(before.get(field) != after.get(field) for field in retained):
                raise ValueError(
                    "Unexpected policy change; inspect retained before/after"
                )
            save(
                args.output,
                {
                    "applied": args.apply,
                    "key_id": token_id,
                    "before_models": before["models"],
                    "after_models": after["models"],
                    "limits_unchanged": True,
                    "tenant": "system",
                    "user": "qa",
                },
            )
            print(
                json.dumps(
                    {
                        "applied": args.apply,
                        "only_added_model": "scvi-scanvi",
                        "limits_unchanged": True,
                    }
                )
            )
        finally:
            client.delete("/admin/api/v1/session")


if __name__ == "__main__":
    main()
