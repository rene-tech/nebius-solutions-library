#!/usr/bin/env python3
"""Capture Apps/Runs/Usage for the dataset parent and its Cosmos children.

All raw admin responses stay in a private operator directory. This observes
existing runs; it does not submit, cancel, scale, or alter customer policy.
"""

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from release_operator import MODELS, check, kube, successful, write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    args.private_output.mkdir(mode=0o700, parents=True, exist_ok=True)
    check(not args.private_output.stat().st_mode & 0o077, "private_directory_required")
    secret = kube(args.kubeconfig, args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json")
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    receipt = {"at": datetime.now(UTC).isoformat(), "principal_id": args.principal_id, "apps": []}
    with httpx.Client(base_url=args.origin, timeout=60, trust_env=False, headers={"origin": args.origin}) as client:
        successful(client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}))
        apps = successful(client.get("/admin/api/v1/apps"))
        write_private(args.private_output / "apps.json", apps, (token,))
        selected = [app for app in apps["data"]["items"] if app["public_model_id"] in MODELS]
        check({app["public_model_id"] for app in selected} == MODELS, "both_apps_required")
        for app in selected:
            model = app["public_model_id"]
            base = "/admin/api/v1/apps/" + app["app_id"]
            observed = {"model": model, "app_id": app["app_id"], "status": app["status"], "endpoints": {}}
            for section in ("runs", "usage", "settings", "containers", "metrics", "logs"):
                params = {"principal_id": args.principal_id, "limit": 200} if section == "runs" else {}
                response = client.get(base + "/" + section, params=params)
                body = successful(response)
                write_private(args.private_output / f"{model}-{section}.json", body, (token,))
                observed["endpoints"][section] = response.status_code
                if section == "runs":
                    observed["runs"] = [
                        {
                            field: row["operation"].get(field)
                            for field in ("id", "model_id", "status", "principal_id", "tenant_id")
                        }
                        for row in body["data"]["items"]
                    ]
                elif section == "usage":
                    observed["usage_scope"] = "app-wide; runs above are filtered to the canary principal"
                    observed["usage"] = {
                        field: body["data"].get(field)
                        for field in ("logical_runs", "succeeded_runs", "failed_runs", "active_runs", "scientific_gpu")
                    }
            receipt["apps"].append(observed)
        client.delete("/admin/api/v1/session")
    write_private(args.private_output / "receipt.json", receipt, (token,))
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
