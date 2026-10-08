"""Real public REST/MCP DiffDock cohort using only the existing system/qa key.

No customer credential, node change, fake GPU execution or direct runtime call.
Writes receipts and native results to a private directory; never prints bodies.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result
from fs2_serve.model_input_contracts import _examples, _resource

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://89.169.99.188"
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "preempted"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def run(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    token = (json.loads(args.key_file.read_text())["secret"] if args.key_file
             else dotenv_values(args.key_env)["SCIENTIFIC_MODELS_API_KEY"])
    assert token
    validator_path = ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/diffdock-native/validate_diffdock.py"
    module = importlib.util.spec_from_file_location("native_validator", validator_path)
    validator = importlib.util.module_from_spec(module)
    module.loader.exec_module(validator)
    fixture = _resource("native-examples.json")["diffdock"]["request"]
    async with httpx2.AsyncClient(
        base_url=ORIGIN, timeout=60, trust_env=False,
        headers={"Authorization": "Bearer " + token, "Origin": ORIGIN},
    ) as http:
        me_response = await http.get("/v1/me")
        me_response.raise_for_status()
        me = me_response.json()
        assert (me["tenant_id"], me["principal_id"]) == ("system", "qa")
        assert me["max_concurrency"] >= 2
        async with Client(streamable_http_client(ORIGIN + "/mcp", http_client=http), mode=MCP_PROTOCOL_VERSION) as client:
            schema = _mcp_result(await client.call_tool("get_model_schema", {"model_id": "diffdock", "protocol": "native"}))
            (args.output / "schema.json").write_text(json.dumps(schema, indent=2))

            async def case(path, seed):
                payload = {**_examples("diffdock")[0], "random_seed": seed}
                idempotency = f"serving-fallback-{args.cohort}-{path}-20261008"

                async def submit():
                    if path == "mcp":
                        return _mcp_result(await client.call_tool("infer_diffdock_native", {
                            **payload, "idempotency_key": idempotency, "wait_seconds": 0,
                        }))
                    response = await http.post("/v1/models/diffdock:invoke", json={
                        "operation": "dock", "payload": payload,
                    }, headers={"Idempotency-Key": idempotency, "x-fs2-wait-seconds": "0"})
                    response.raise_for_status()
                    return response.json()

                started = time.monotonic()
                accepted = await submit()
                operation_id = accepted["id"]
                (args.output / f"{path}-accepted.json").write_text(json.dumps(accepted, indent=2))
                print(json.dumps({"path": path, "id": operation_id, "status": accepted["status"]}), flush=True)
                replay = await submit()
                assert replay["id"] == operation_id
                end = time.monotonic() + 1800
                last_status = None
                while time.monotonic() < end:
                    if path == "mcp":
                        operation = _mcp_result(await client.call_tool("get_operation", {"operation_id": operation_id}))
                    else:
                        response = await http.get(f"/v1/operations/{operation_id}")
                        response.raise_for_status()
                        operation = response.json()
                    if operation["status"] != last_status:
                        print(json.dumps({"path": path, "id": operation_id, "status": operation["status"]}), flush=True)
                        last_status = operation["status"]
                    if operation["status"] in TERMINAL:
                        break
                    await asyncio.sleep(10)
                (args.output / f"{path}-terminal.json").write_text(json.dumps(operation, indent=2))
                assert operation["status"] == "succeeded", (operation_id, operation["status"], operation.get("error_code"))
                assert operation["tenant_id"] == "system" and operation["principal_id"] == "qa"
                # Fetch native bytes over the ordinary REST result path; MCP
                # large-result envelopes may be artifact references, not bytes.
                response = await http.get(f"/v1/operations/{operation_id}/result")
                response.raise_for_status()
                result = response.json()
                native = result.get("result", result)
                validation = validator._validate_response(native, fixture)
                (args.output / f"{path}-result.json").write_text(json.dumps(result))
                receipt = {
                    "operation_id": operation_id, "path": path, "status": operation["status"],
                    "payload_sha256": digest(payload), "result_sha256": digest(result),
                    "validation": validation, "idempotency_passed": True,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "operation": operation,
                }
                (args.output / f"{path}-receipt.json").write_text(json.dumps(receipt, indent=2))
                print(json.dumps({"path": path, "id": operation_id, "semantic_validation": "passed", "seconds": receipt["elapsed_seconds"]}), flush=True)
                return receipt

            receipts = await asyncio.gather(case("rest", 10801), case("mcp", 10802))
            (args.output / "cohort.json").write_text(json.dumps({
                "cohort": args.cohort, "identity": me, "receipts": receipts,
                "scope": "public REST and typed MCP DiffDock cold-pool fallback; not LibreChat qualification",
            }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    keys = parser.add_mutually_exclusive_group(required=True)
    keys.add_argument("--key-env", type=Path)
    keys.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", required=True)
    asyncio.run(run(parser.parse_args()))
