#!/usr/bin/env python3
"""Create one bounded task-owned customer key; retain public live contracts.

The private key file is create-exclusive (0600), never printed, and reused on
resume. No existing customer/key/model settings are changed. The task-owned
canary user must already exist and be enabled.
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import httpx2
from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TENANT = "fs2-starter-data-acceptance-20260920"
PRINCIPAL = "seed-canary"
SCOPES = [
    "catalog.read",
    "inference.invoke",
    "mcp.invoke",
    "operations.read",
    "operations.result",
    "operations.cancel",
    "artifacts.write",
]


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    if args.key_file.exists():
        access = json.loads(args.key_file.read_bytes())
        assert access["origin"] == args.origin and access["tenant_id"] == TENANT
    else:
        secret = json.loads(
            subprocess.check_output(
                [
                    "kubectl",
                    "--kubeconfig",
                    args.kubeconfig,
                    "--context",
                    args.context,
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
        with httpx.Client(
            base_url=args.origin,
            headers={"origin": args.origin},
            timeout=60,
            trust_env=False,
        ) as admin:
            response = admin.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            )
            response.raise_for_status()

            def call(method, path, value=None):
                response = admin.request(method, path, json=value)
                response.raise_for_status()
                return response.json()["data"]

            users = call("GET", "/admin/api/v1/users?limit=1000")["items"]
            user = next(
                u
                for u in users
                if u["tenant_id"] == TENANT and u["principal_id"] == PRINCIPAL
            )
            assert user["enabled"], "do not reactivate a disabled canary"
            call("PATCH", "/admin/api/v1/users/" + user["id"], {"app_ids": None})
            public = httpx.get(
                "https://forge.nebius.cloud/api/models", timeout=60
            ).json()
            assert public["ok"]
            models = sorted(m["id"] for m in public["models"])
            disclosure = call(
                "POST",
                f"/admin/api/v1/users/{user['id']}/keys",
                {
                    "name": "customer-starter-data-20260920",
                    "tenant_id": TENANT,
                    "principal_id": PRINCIPAL,
                    "models": models,
                    "scopes": SCOPES,
                    "max_concurrency": 4,
                    "request_budget": 800,
                    "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                },
            )
            access = {
                "origin": args.origin,
                "tenant_id": TENANT,
                "principal_id": PRINCIPAL,
                "user_id": user["id"],
                "key_id": disclosure["key"]["id"],
                "secret": disclosure["secret"],
            }
            fd = os.open(args.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(access, output)
    if args.repair_test_scopes or args.refresh_test_models:
        assert access["tenant_id"] == TENANT and access["principal_id"] == PRINCIPAL
        secret = json.loads(
            subprocess.check_output(
                [
                    "kubectl",
                    "--kubeconfig",
                    args.kubeconfig,
                    "--context",
                    args.context,
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
        with httpx.Client(
            base_url=args.origin, headers={"origin": args.origin}, timeout=60
        ) as admin:
            admin.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            ).raise_for_status()
            patch = {"scopes": SCOPES} if args.repair_test_scopes else {}
            if args.refresh_test_models:
                catalog = httpx.get(
                    "https://forge.nebius.cloud/api/models", timeout=60
                ).json()
                assert catalog["ok"]
                patch["models"] = sorted(model["id"] for model in catalog["models"])
            admin.patch(
                "/admin/api/v1/keys/" + access["key_id"], json=patch
            ).raise_for_status()
    with httpx.Client(
        base_url=args.origin,
        headers={"authorization": "Bearer " + access["secret"]},
        timeout=60,
    ) as api:
        native = api.get("/v1/models")
        native.raise_for_status()
        batch = api.get("/v1/scientific-models")
        batch.raise_for_status()
        me = api.get("/v1/me")
        me.raise_for_status()
        for name, data in (
            ("native-catalog", native.json()),
            ("scientific-catalog", batch.json()),
            ("canary-policy", me.json()),
        ):
            (args.output / (name + ".json")).write_text(
                json.dumps(data, indent=2) + "\n"
            )
    website = httpx.get("https://forge.nebius.cloud/api/models", timeout=60).json()
    metadata = httpx.get(
        "https://forge.nebius.cloud/api/catalog-metadata.json", timeout=60
    ).json()
    (args.output / "website-catalog.json").write_text(
        json.dumps(website, indent=2) + "\n"
    )
    (args.output / "website-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    async with httpx2.AsyncClient(
        headers={"authorization": "Bearer " + access["secret"], "origin": args.origin},
        timeout=90,
    ) as http:
        async with Client(
            streamable_http_client(args.origin + "/mcp", http_client=http),
            mode=MCP_PROTOCOL_VERSION,
        ) as client:
            for model in website["models"]:
                try:
                    response = await client.call_tool(
                        "get_model_schema", {"model_id": model["id"]}
                    )
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "model_id": model["id"],
                                "code": getattr(exc, "code", None),
                                "message": getattr(exc, "message", str(exc)),
                                "data": getattr(exc, "data", None),
                            }
                        ),
                        flush=True,
                    )
                    continue
                data = _mcp_result(response)
                (args.output / (model["id"] + ".json")).write_text(
                    json.dumps(data, indent=2) + "\n"
                )
                print(
                    json.dumps(
                        {
                            "model_id": model["id"],
                            "contracts": len(data.get("contracts", [])),
                        }
                    ),
                    flush=True,
                )
    print(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "key_id": access["key_id"],
                "models": len(website["models"]),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repair-test-scopes", action="store_true")
    parser.add_argument("--refresh-test-models", action="store_true")
    asyncio.run(main(parser.parse_args()))
