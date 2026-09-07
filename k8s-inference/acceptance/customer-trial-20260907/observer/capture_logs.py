#!/usr/bin/env python3
"""Read a bounded installed-Loki window; keep log payloads private."""

import argparse
import hashlib
import json
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    grafana = access["endpoints"]["grafana_url"].rstrip("/")
    credentials = access["credentials"]["grafana"]
    auth = httpx.BasicAuth(credentials["username"], credentials["password"])
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, timeout=30
    ) as client:
        client.post(
            "/admin/api/v1/session",
            headers={
                "authorization": "Bearer "
                + access["credentials"]["admin_bootstrap_token"]
            },
        ).raise_for_status()
        try:
            sources = client.get(grafana + "/api/datasources", auth=auth)
            sources.raise_for_status()
            source = next(row for row in sources.json() if row["type"] == "loki")
            proxy = (
                grafana
                + "/api/datasources/proxy/uid/"
                + source["uid"]
                + "/loki/api/v1/query_range"
            )
            reply = client.get(
                proxy,
                params={
                    "start": args.start,
                    "end": args.end,
                    "query": args.query,
                    "limit": 5000,
                    "direction": "forward",
                },
                auth=auth,
            )
            reply.raise_for_status()
            result = {
                "start": args.start,
                "end": args.end,
                "query": args.query,
                "response": reply.json(),
            }
        finally:
            client.delete("/admin/api/v1/session")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "streams": len(result["response"]["data"]["result"]),
                "lines": sum(
                    len(row["values"]) for row in result["response"]["data"]["result"]
                ),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
