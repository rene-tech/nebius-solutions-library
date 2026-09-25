#!/usr/bin/env python3
"""Read-only public identity, named MCP contract and fixture preflight."""
import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path

import httpx
import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from run_acceptance import TENANT, call, digest, now, ordinary_policy, prepare_parameters, read, require, save


async def schema(endpoint, secret):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + secret}, timeout=60, trust_env=False) as http:
        async with Client(streamable_http_client(endpoint, http_client=http), read_timeout_seconds=60) as client:
            tools = (await client.list_tools()).tools
            selected = [tool for tool in tools if tool.name == "submit_gromacs_workflow"]
            require(len(selected) == 1, "typed_gromacs_tool_missing")
            return selected[0].input_schema, sorted(tool.name for tool in tools)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--endpoint", default="https://89.169.99.188/mcp")
    parser.add_argument("--tenant", default=TENANT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    key = read(args.key_file)
    require(key.get("disposable") is True and key["key"]["tenant_id"] == args.tenant, "wrong_key_owner")
    origin = args.endpoint.removesuffix("/mcp")
    with httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"Authorization": "Bearer " + key["secret"]}) as http:
        response = http.get("/v1/me")
        require(response.status_code == 200, "ordinary_identity_unavailable")
        policy = ordinary_policy(response.json(), args.tenant)
        operations = http.get("/v1/operations", params={"limit": 200})
        require(operations.status_code == 200 and not operations.json()["next_cursor"], "operation_inventory_unavailable")
        existing = len(operations.json()["data"])
    parameters, fixture = prepare_parameters(args.fixture, ["window-01", "window-02"])
    contract, names = asyncio.run(schema(args.endpoint, key["secret"]))
    Draft202012Validator(contract).evolve(schema=contract["properties"]["parameters"]).validate(parameters)
    require({"get_scientific_status", "get_scientific_result", "cancel_scientific_run"} <= set(names), "lifecycle_tools_missing")
    discovered = call(args.endpoint, key["secret"], "get_model_schema", {"model_id": "gromacs", "tool_name": "submit_gromacs_workflow"})
    receipt = {"schema": "fs2-serve.nebius.ai/admitted-pool-recovery-preflight/v1", "state": "schema-validated",
               "at": now(), "endpoint": args.endpoint, "tenant_policy": policy, "key_id": key["key"]["id"],
               "mcp_version": importlib.metadata.version("mcp"), "tool_schema_sha256": digest(contract),
               "model_contract_sha256": digest(discovered), "fixture": fixture,
               "existing_operations": existing, "inference_submitted": False, "uploads_created": False, "kubernetes_mutations": False}
    save(args.output, receipt)
    print(json.dumps({"state": receipt["state"], "tenant_id": args.tenant, "existing_operations": existing,
                      "tool_schema_sha256": receipt["tool_schema_sha256"]}))


if __name__ == "__main__":
    main()
