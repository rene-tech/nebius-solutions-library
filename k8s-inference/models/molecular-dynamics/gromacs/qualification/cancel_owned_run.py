"""Cancel one running qualification operation through the customer MCP tool.

Read-only Kubernetes checks prove its GPU Pods are removed. No shared workload,
node, key, or quota is changed. The saved receipt prevents repeated cancellation.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def unpack(response):
    data = response.model_dump(mode="json", by_alias=True)
    if data.get("isError"):
        raise RuntimeError(
            "The customer MCP tool returned an error; retain the operation for diagnosis."
        )
    if data.get("structuredContent") is not None:
        return data["structuredContent"]
    return json.loads(
        next(item["text"] for item in data["content"] if item["type"] == "text")
    )


async def run(args):
    if args.output.exists():
        raise ValueError(
            "This cancellation test already has a receipt; do not repeat it blindly."
        )
    key = json.loads(args.key_file.read_text())
    deadline = time.monotonic() + 900
    status_file = args.receipt / "status.json"
    async with httpx2.AsyncClient(
        headers={"authorization": "Bearer " + key["secret"]},
        timeout=90,
        trust_env=False,
    ) as http:
        async with Client(
            streamable_http_client("https://89.169.99.188/mcp", http_client=http)
        ) as client:
            while time.monotonic() < deadline:
                if not status_file.exists():
                    await asyncio.sleep(2)
                    continue
                status = json.loads(status_file.read_text())
                operation = status["operation"]
                assert operation["model_id"] == args.model
                assert operation["tenant_id"] == operation["principal_id"] == "rene"
                assert operation["idempotency_key"] == args.idempotency_key
                assert operation["token_id"] == key["key"]["id"]
                if operation["status"] in {"failed", "cancelled", "succeeded"}:
                    raise RuntimeError(
                        "Operation ended before the running-cancellation test."
                    )
                attempts = [a for s in status["batch"]["stages"] for a in s["attempts"]]
                if not any(a["last_phase"] == "active_compute" for a in attempts):
                    await asyncio.sleep(2)
                    continue
                requested_at = datetime.now(timezone.utc)
                record = {
                    "operation_id": operation["id"],
                    "requested_at": requested_at.isoformat(),
                    "before": status,
                    "tool": "cancel_scientific_run",
                }
                with os.fdopen(
                    os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w",
                ) as f:
                    json.dump(record, f, indent=2)
                record["response"] = unpack(
                    await client.call_tool(
                        "cancel_scientific_run", {"operation_id": operation["id"]}
                    )
                )
                while time.monotonic() < deadline:
                    final = unpack(
                        await client.call_tool(
                            "get_scientific_status", {"operation_id": operation["id"]}
                        )
                    )
                    attempts = [
                        a for s in final["batch"]["stages"] for a in s["attempts"]
                    ]
                    if final["operation"]["status"] == "cancelled" and all(
                        a["resource_released"] for a in attempts
                    ):
                        kube = [
                            "kubectl",
                            "--kubeconfig",
                            args.kubeconfig,
                            "--context",
                            args.context,
                            "-n",
                            "fs2-models",
                        ]
                        remaining = []
                        for attempt in attempts:
                            selector = (
                                "jobset.sigs.k8s.io/jobset-name="
                                if attempt["workload_kind"] == "JobSet"
                                else "batch.kubernetes.io/job-name="
                            )
                            pods = json.loads(
                                subprocess.check_output(
                                    kube
                                    + [
                                        "get",
                                        "pods",
                                        "-l",
                                        selector + attempt["workload_name"],
                                        "-o",
                                        "json",
                                    ]
                                )
                            )["items"]
                            remaining.extend(p["metadata"]["name"] for p in pods)
                        if not remaining:
                            record.update(
                                final=final,
                                remaining_pods=remaining,
                                seconds_to_gpu_release=(
                                    datetime.now(timezone.utc) - requested_at
                                ).total_seconds(),
                            )
                            # This is a generated test receipt, not a source-file edit.
                            args.output.write_text(json.dumps(record, indent=2) + "\n")
                            print(
                                json.dumps(
                                    {
                                        k: record[k]
                                        for k in (
                                            "operation_id",
                                            "seconds_to_gpu_release",
                                            "remaining_pods",
                                        )
                                    }
                                )
                            )
                            return
                    await asyncio.sleep(3)
    raise TimeoutError(
        "Cancellation or resource release did not complete; inspect the saved operation."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument(
        "--model", choices=["gromacs", "gromacs-mpi"], default="gromacs"
    )
    args = parser.parse_args()
    if not args.idempotency_key.startswith("gromacs-qualified-cancellation-20260923-"):
        raise ValueError("Only this task-owned cancellation fixture is in scope.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
