"""Read-only exact generated startup/demand queries plus retained Pod events."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=15s",
        "-n",
        "fs2-models",
        "get",
    ]

    def get(*parts):
        result = subprocess.run(
            [*kube, *parts, "-o", "json"], capture_output=True, text=True, timeout=20
        )  # noqa: S603
        if result.returncode:
            return {
                "returncode": result.returncode,
                "error": "read-only Kubernetes query failed",
            }
        return {"returncode": 0, "data": json.loads(result.stdout)}

    result = {"captured_at": datetime.now(UTC).isoformat()}
    result["scaled_object"] = get(
        "scaledobjects.keda.sh", "fs2-model-qwen3-8b-b300-burst-h100-1x"
    )
    result["events"] = get(
        "events", "--field-selector", "involvedObject.uid=" + args.pod_uid
    )
    access = json.loads(args.credentials.read_bytes())
    grafana = access["endpoints"]["grafana_url"].rstrip("/")
    credentials = access["credentials"]["grafana"]
    result["queries"] = []
    with httpx.Client(
        auth=(credentials["username"], credentials["password"]), timeout=25
    ) as client:
        response = client.get(grafana + "/api/datasources")
        response.raise_for_status()
        source = next(item for item in response.json() if item["type"] == "prometheus")
        endpoint = (
            grafana + "/api/datasources/proxy/uid/" + source["uid"] + "/api/v1/query"
        )
        for trigger in (
            result["scaled_object"].get("data", {}).get("spec", {}).get("triggers", [])
        ):
            metadata = trigger.get("metadata", {})
            query = metadata.get("query")
            if trigger.get("type") != "prometheus" or not query:
                continue
            response = client.get(endpoint, params={"query": query})
            result["queries"].append(
                {
                    "metric_name": metadata["metricName"],
                    "query": query,
                    "http_status": response.status_code,
                    "response": response.json(),
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "captured_at": result["captured_at"],
                "event_count": len(result["events"].get("data", {}).get("items", [])),
                "queries": [
                    {
                        "metric_name": row["metric_name"],
                        "http_status": row["http_status"],
                        "results": row["response"].get("data", {}).get("result", []),
                    }
                    for row in result["queries"]
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
