#!/usr/bin/env python3
"""Bounded read-only Apps/Users/Capacity acceptance receipts, stored privately.

Creates and closes only its own normal admin login session. No model invocations,
key/configuration changes, cluster mutations or direct database access. The caller
must have the release owner's explicit start signal. Credentials are read from
the existing local bundle and never included in output or process arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def pod_inventory(item: dict) -> dict:
    metadata, spec = item.get("metadata", {}), item.get("spec", {})
    return {
        "metadata": {
            key: metadata[key]
            for key in (
                "name",
                "namespace",
                "uid",
                "creationTimestamp",
                "deletionTimestamp",
                "labels",
                "ownerReferences",
            )
            if key in metadata
        },
        "spec": {
            "nodeName": spec.get("nodeName"),
            "overhead": spec.get("overhead", {}),
            **{
                field: [
                    {
                        key: row[key]
                        for key in ("name", "resources", "restartPolicy")
                        if key in row
                    }
                    for row in spec.get(field, [])
                ]
                for field in ("containers", "initContainers")
            },
        },
        "status": item.get("status", {}),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--from", dest="start", default="2026-09-08T08:59:00Z")
    parser.add_argument("--to", dest="end", default="2026-09-08T10:00:00Z")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; use a fresh receipt name.")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    grafana = access["endpoints"]["grafana_url"].rstrip("/")
    grafana_auth = httpx.BasicAuth(
        access["credentials"]["grafana"]["username"],
        access["credentials"]["grafana"]["password"],
    )
    result = {
        "release": args.release,
        "started_at": datetime.now(UTC).isoformat(),
        "window": {"from": args.start, "to": args.end},
        "context": args.context,
        "http": {},
        "kubernetes": {},
    }
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(
        base_url=origin, headers={"origin": origin}, timeout=30
    ) as client:

        async def capture(key, path, *, params=None, auth=None):
            async with semaphore:
                started = time.monotonic()
                try:
                    reply = await client.get(path, params=params, auth=auth)
                    receipt = {
                        "status": reply.status_code,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                    try:
                        receipt["data"] = reply.json()
                    except ValueError:
                        receipt["body_bytes"] = len(reply.content)
                except httpx.HTTPError as error:
                    receipt = {
                        "error": type(error).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                result["http"][key] = receipt
                return receipt

        async def kube(key, resource):
            process = await asyncio.create_subprocess_exec(
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "--context",
                args.context,
                "--request-timeout=20s",
                "get",
                resource,
                "-A",
                "-o",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                body, _ = await asyncio.wait_for(process.communicate(), timeout=25)
                if process.returncode:
                    receipt = {
                        "error": "kubectl_failed",
                        "returncode": process.returncode,
                    }
                else:
                    items = json.loads(body)["items"]
                    receipt = {
                        "items": [pod_inventory(item) for item in items]
                        if key == "pods"
                        else items
                    }
            except TimeoutError:
                process.kill()
                await process.wait()
                receipt = {"error": "kubectl_timeout"}
            result["kubernetes"][key] = receipt

        login = await client.post(
            "/admin/api/v1/session",
            headers={
                "authorization": "Bearer "
                + access["credentials"]["admin_bootstrap_token"]
            },
        )
        result["login_status"] = login.status_code
        try:
            login.raise_for_status()
            selected = {"from": args.start, "to": args.end}
            await asyncio.gather(
                capture("apps", "/admin/api/v1/apps", params=selected),
                capture("users", "/admin/api/v1/users", params=selected),
                capture("capacity_summary", "/admin/api/v1/capacity/summary"),
                capture("capacity", "/admin/api/v1/capacity"),
                capture("inventory", "/admin/api/v1/model-inventory"),
                capture(
                    "operations",
                    "/admin/api/v1/operations",
                    params={**selected, "limit": 200},
                ),
                capture(
                    "scientific",
                    "/admin/api/v1/scientific-runs",
                    params={**selected, "limit": 200},
                ),
                kube("nodes", "nodes"),
                kube("pods", "pods"),
                kube("workloads", "workloads.kueue.x-k8s.io"),
            )
            apps = (
                result["http"]
                .get("apps", {})
                .get("data", {})
                .get("data", {})
                .get("items", [])
            )
            await asyncio.gather(
                *(
                    capture(
                        f"usage:{app['app_id']}",
                        f"/admin/api/v1/apps/{app['app_id']}/usage",
                        params=selected,
                    )
                    for app in apps[:100]
                )
            )
            for state in ("queued", "activating", "running"):
                await capture(
                    f"current_operations:{state}",
                    "/admin/api/v1/operations",
                    params={"status": state, "limit": 200},
                )
            sources = await capture(
                "datasources", grafana + "/api/datasources", auth=grafana_auth
            )
            prometheus = next(
                (
                    row
                    for row in sources.get("data", [])
                    if row.get("type") == "prometheus"
                ),
                None,
            )
            if prometheus:
                endpoint = (
                    grafana
                    + "/api/datasources/proxy/uid/"
                    + prometheus["uid"]
                    + "/api/v1/query"
                )
                await capture(
                    "gpu_utilization",
                    endpoint,
                    params={"query": "avg(max by(UUID) (DCGM_FI_DEV_GPU_UTIL))"},
                    auth=grafana_auth,
                )
        except Exception as error:
            # Preserve failed acceptance without dumping credential-bearing request objects.
            result["error"] = type(error).__name__
        finally:
            result["logout_status"] = (
                await client.delete("/admin/api/v1/session")
            ).status_code
    result["completed_at"] = datetime.now(UTC).isoformat()
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "started_at": result["started_at"],
                "completed_at": result["completed_at"],
                "release": args.release,
                "http_statuses": {
                    key: value.get("status", value.get("error"))
                    for key, value in result["http"].items()
                },
                "kubernetes_counts": {
                    key: len(value["items"]) if "items" in value else None
                    for key, value in result["kubernetes"].items()
                },
                "error": result.get("error"),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
