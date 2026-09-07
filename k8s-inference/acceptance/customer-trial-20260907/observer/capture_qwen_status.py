#!/usr/bin/env python3
"""Read only the historical Qwen status rows surrounding the trial MCP error."""

import argparse
import json
from pathlib import Path

import httpx


QUERY = """
SELECT observed_at, recorded_at, source_resource_version, status::text
FROM fs2_model_deployment_status_events
WHERE namespace='fs2-models' AND name='qwen3-8b'
AND observed_at BETWEEN '2026-09-07T15:11:45Z' AND '2026-09-07T15:12:20Z'
ORDER BY observed_at, recorded_at LIMIT 100
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
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
            source = next(
                row
                for row in sources.json()
                if row["type"] == "grafana-postgresql-datasource"
            )
            reply = client.post(
                grafana + "/api/ds/query",
                auth=auth,
                json={
                    "from": "1788793905000",
                    "to": "1788793940000",
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {
                                "uid": source["uid"],
                                "type": source["type"],
                            },
                            "rawSql": QUERY,
                            "format": "table",
                        }
                    ],
                },
            )
            result = {
                "status": reply.status_code,
                "query": QUERY,
                "response": reply.json(),
            }
        finally:
            client.delete("/admin/api/v1/session")
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "http_status": result["status"],
                "query_status": result["response"]
                .get("results", {})
                .get("A", {})
                .get("status"),
                "error": result["response"]
                .get("results", {})
                .get("A", {})
                .get("error"),
            }
        )
    )


if __name__ == "__main__":
    main()
