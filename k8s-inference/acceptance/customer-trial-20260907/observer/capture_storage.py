#!/usr/bin/env python3
"""Read historical/current root-disk byte metrics from the installed Prometheus."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--at", required=True, help="RFC3339 time or Unix seconds")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    grafana = access["endpoints"]["grafana_url"].rstrip("/")
    credentials = access["credentials"]["grafana"]
    auth = httpx.BasicAuth(credentials["username"], credentials["password"])
    result = {
        "captured_at": datetime.now(UTC).isoformat(),
        "metric_at": args.at,
        "responses": {},
    }
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
            source_reply = client.get(grafana + "/api/datasources", auth=auth)
            source_reply.raise_for_status()
            source = next(
                row for row in source_reply.json() if row["type"] == "prometheus"
            )
            proxy = (
                grafana
                + "/api/datasources/proxy/uid/"
                + source["uid"]
                + "/api/v1/query"
            )
            for key, query in {
                "available_bytes": 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
                "size_bytes": 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
                "node_identity": "node_uname_info",
            }.items():
                reply = client.get(
                    proxy, params={"query": query, "time": args.at}, auth=auth
                )
                result["responses"][key] = {
                    "status": reply.status_code,
                    "data": reply.json(),
                }
        finally:
            client.delete("/admin/api/v1/session")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "metric_at": args.at,
                "statuses": {
                    key: value["status"] for key, value in result["responses"].items()
                },
            }
        )
    )


if __name__ == "__main__":
    main()
