#!/usr/bin/env python3
"""Read-only release surface checks; creates only an expiring operator session.

Run with the control-plane environment (httpx required). The browser state is a
credential and is written mode 0600 below a private operator-owned directory.
No inference, user/key changes, storage provisioning or scaling is performed.
Passing this check establishes API reachability, not customer model readiness.
"""

import argparse
import base64
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


def private_json(path: Path, value: dict) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    args.private_output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.private_output.stat().st_mode & 0o077:
        parser.error("output directory must be private (mode 0700)")
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
               "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
    secret = json.loads(subprocess.check_output(command))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    checks = []
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin},
                      timeout=60, trust_env=False) as client:
        login = client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
        if login.status_code != 200:
            raise SystemExit(f"operator login failed: HTTP {login.status_code}")
        for path in ["/readyz", "/admin/", "/admin/api/v1/overview",
                     "/admin/api/v1/models", "/admin/api/v1/apps", "/admin/api/v1/users",
                     "/admin/api/v1/keys", "/admin/api/v1/capacity/summary",
                     "/admin/api/v1/observability"]:
            response = client.get(path)
            check = {"path": path, "http_status": response.status_code,
                     "duration_ms": round(response.elapsed.total_seconds() * 1000)}
            if response.headers.get("content-type", "").startswith("application/json"):
                body = response.json()
                data = body.get("data", body) if isinstance(body, dict) else body
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    check["item_count"] = len(data["items"])
            checks.append(check)
        cookies = [{"name": c.name, "value": c.value, "domain": urlparse(args.origin).hostname,
                    "path": c.path, "expires": c.expires or -1, "httpOnly": True,
                    "secure": c.secure, "sameSite": "Strict"} for c in client.cookies.jar]
        private_json(args.private_output / "browser-state.json", {"cookies": cookies, "origins": []})
    report = {"at": datetime.now(UTC).isoformat(), "origin": args.origin,
              "scope": "operator_read_only_surface_not_model_qualification", "checks": checks,
              "all_http_200": all(check["http_status"] == 200 for check in checks)}
    private_json(args.private_output / "surface-receipt.json", report)
    print(json.dumps(report, indent=2))
    if not report["all_http_200"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
