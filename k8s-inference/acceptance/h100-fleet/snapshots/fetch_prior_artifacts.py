#!/usr/bin/env python3
"""Retain authorized prior scientific operation/artifact bytes for replay."""

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--admin-detail", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scientific-fleet"))
    from run_acceptance import PublicApiClient

    bundle = json.loads(args.outputs.read_bytes())
    client = PublicApiClient(
        bundle["endpoints"]["inference_base_url"].removesuffix("/v1"),
        bundle["credentials"]["scientific_access_token"],
    )
    receipts = []
    paths = [
        ("operation.json", f"/v1/operations/{args.operation}"),
        ("result.json", f"/v1/operations/{args.operation}/result"),
    ]
    paths.extend(
        (artifact + ".bin", f"/v1/artifacts/{artifact}/content")
        for artifact in args.artifact
    )
    for name, path in paths:
        response = client.request("GET", path)
        if response.status != 200:
            raise RuntimeError(
                f"authorized retained input unavailable: {name}, HTTP {response.status}"
            )
        (args.directory / name).write_bytes(response.body)
        receipts.append(
            {
                "name": name,
                "bytes": len(response.body),
                "sha256": hashlib.sha256(response.body).hexdigest(),
            }
        )
    if args.admin_detail:
        origin = bundle["endpoints"]["inference_base_url"].removesuffix("/v1")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        session = origin + "/admin/api/v1/session"
        with opener.open(
            urllib.request.Request(
                session,
                method="POST",
                headers={
                    "Authorization": "Bearer "
                    + bundle["credentials"]["admin_bootstrap_token"],
                    "Origin": origin,
                },
            ),
            timeout=40,
        ):
            pass
        try:
            with opener.open(
                origin + "/admin/api/v1/scientific-runs/" + args.operation, timeout=40
            ) as response:
                (args.directory / "admin-detail.json").write_bytes(response.read())
        finally:
            with opener.open(
                urllib.request.Request(
                    session, method="DELETE", headers={"Origin": origin}
                ),
                timeout=40,
            ):
                pass
    (args.directory / "receipt.json").write_text(json.dumps(receipts, indent=2))
    print(json.dumps(receipts))


if __name__ == "__main__":
    main()
