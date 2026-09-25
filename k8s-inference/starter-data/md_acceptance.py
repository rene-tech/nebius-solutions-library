#!/usr/bin/env python3
"""Customer-scoped MD starter acceptance; never print or commit credentials.

Prepare creates only this campaign's disposable owner/key and default bucket.
All simulations use that ordinary key through public MCP, not the admin session.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import run_example as runner

TENANT = "md-starter-acceptance-20260925"
TENANTS = (TENANT, "md-starter-new-workspace-20260925")
PRINCIPAL = "seed-canary"
MODELS = ["gromacs", "namd", "amber", "lammps"]
SCOPES = [
    "catalog.read",
    "inference.invoke",
    "mcp.invoke",
    "operations.read",
    "operations.result",
    "operations.cancel",
    "artifacts.write",
]


def checked(response):
    if response.is_error:
        raise RuntimeError(f"http_{response.status_code}_{response.request.url.path}")
    return response.json()


@contextmanager
def admin_session(args):
    secret = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "--context",
                args.context,
                "--request-timeout=30s",
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
    api = httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        timeout=60,
        trust_env=False,
    )
    checked(
        api.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
    )
    try:
        yield api
    finally:
        api.delete("/admin/api/v1/session")
        api.close()


async def prepare(args):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = args.output / "access-private.json"
    with admin_session(args) as admin:
        if private.exists():
            access = json.loads(private.read_bytes())
            if access["tenant_id"] != args.tenant:
                raise ValueError("wrong_canary_tenant")
        else:
            users = checked(
                admin.get("/admin/api/v1/users", params={"tenant_id": args.tenant})
            )["data"]["items"]
            if users:
                raise ValueError("canary_already_exists_without_local_receipt")
            user = checked(
                admin.post(
                    "/admin/api/v1/users",
                    json={
                        "tenant_id": args.tenant,
                        "principal_id": PRINCIPAL,
                        "kind": "service",
                        "display_name": "MD starter-pack acceptance (temporary)",
                        "enabled": True,
                        "app_ids": None,
                    },
                )
            )["data"]
            # Persist the owner before key creation, preserving partial provisioning.
            runner.save(args.output / "owner-private.json", user)
            issued = checked(
                admin.post(
                    f"/admin/api/v1/users/{user['id']}/keys",
                    json={
                        "tenant_id": args.tenant,
                        "principal_id": PRINCIPAL,
                        "name": "md-starter-v3-20260925",
                        "models": MODELS,
                        "scopes": SCOPES,
                        "max_concurrency": 3,
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=24)
                        ).isoformat(),
                    },
                )
            )["data"]
            access = {
                "origin": args.origin,
                "tenant_id": args.tenant,
                "principal_id": PRINCIPAL,
                "user_id": user["id"],
                "key_id": issued["key"]["id"],
                "secret": issued["secret"],
                "disposable": True,
            }
            runner.save(private, access)
            runner.save(args.output / "key-policy.json", issued["key"])
        storage = checked(
            admin.get(f"/admin/api/v1/users/{access['user_id']}/storage")
        )["data"]
        runner.save(args.output / "storage.json", storage)
        admin.delete("/admin/api/v1/session")
    contracts = args.output / "contracts"
    contracts.mkdir(exist_ok=True)
    async with httpx.AsyncClient(
        headers={"authorization": "Bearer " + access["secret"], "origin": args.origin},
        timeout=120,
        trust_env=False,
    ) as http:
        runner.save(
            args.output / "customer-policy.json",
            checked(await http.get(args.origin + "/v1/me")),
        )
        async with Client(
            streamable_http_client(args.origin + "/mcp", http_client=http),
            mode="2026-07-28",
        ) as client:
            for model in MODELS:
                value = runner.unpack(
                    await client.call_tool("get_model_schema", {"model_id": model})
                )
                runner.save(contracts / f"{model}.json", value)
                print(
                    json.dumps(
                        {
                            "model": model,
                            "tools": [c["tool_name"] for c in value["contracts"]],
                        }
                    ),
                    flush=True,
                )
    print(
        json.dumps(
            {
                "tenant": args.tenant,
                "key_id": access["key_id"],
                "storage_state": storage["state"],
            }
        ),
        flush=True,
    )


async def campaign(args):
    access = json.loads(args.access.read_bytes())
    if access["tenant_id"] not in TENANTS or not access.get("disposable"):
        raise ValueError("only_disposable_campaign_access_allowed")
    manifest = json.loads((args.pack / "manifest.json").read_bytes())
    # Exercise the customer's packaged client, not a potentially newer checkout.
    packaged_client = args.pack / "run-example.py"
    entry = next(o for o in manifest["objects"] if o["path"] == "run-example.py")
    if runner.file_sha(packaged_client) != entry["sha256"]:
        raise ValueError("packaged_client_checksum_mismatch")
    spec = importlib.util.spec_from_file_location(
        "packaged_starter_runner", packaged_client
    )
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    semaphore = asyncio.Semaphore(args.workers)
    results = []
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def one(case, model):
        async with semaphore:
            folder = args.output / case["id"] / model
            print(
                json.dumps(
                    {"event": "start", "case_id": case["id"], "model_id": model}
                ),
                flush=True,
            )
            try:
                record = await client.run(
                    args.pack,
                    case["id"],
                    model,
                    folder,
                    endpoint=access["origin"] + "/mcp",
                    key=access["secret"],
                    observe_seconds=args.observe_seconds,
                )
                result = {
                    "case_id": case["id"],
                    "model_id": model,
                    "state": record["state"],
                    "operation_id": record.get("operation_id"),
                    "runner_sha256": entry["sha256"],
                }
            except Exception as exc:  # noqa: BLE001 -- bounded payload-free campaign evidence
                result = {
                    "case_id": case["id"],
                    "model_id": model,
                    "state": "client-error",
                    "error_type": type(exc).__name__,
                }
                if (
                    isinstance(exc, (ValueError, RuntimeError))
                    and str(exc).replace("_", "").isalnum()
                ):
                    result["error_code"] = str(exc)[:180]
            results.append(result)
            selection = runner.sha(runner.encoded([args.cases, args.models]))[:12]
            runner.save(
                args.output / ("campaign-" + selection + ".json"),
                {"key_id": access["key_id"], "results": results},
            )
            print(json.dumps(result), flush=True)

    await asyncio.gather(
        *(
            one(case, model)
            for case in manifest["cases"]
            if case["category"] == "molecular-dynamics"
            and (not args.cases or case["id"] in args.cases)
            for model in case["compatible_model_ids"]
            if not args.models or model in args.models
        )
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--tenant", choices=TENANTS, default=TENANT)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--access", type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--observe-seconds", type=int, default=14400)
    parser.add_argument("--cases", nargs="*")
    parser.add_argument("--models", nargs="*")
    args = parser.parse_args()
    asyncio.run(prepare(args) if args.action == "prepare" else campaign(args))


if __name__ == "__main__":
    main()
