#!/usr/bin/env python3
"""Typed MCP acceptance; offline unless --execute after an exact release GO.

Inspect every authorized serving/scientific model contract. Submit four original
bounded synthetic serving inputs, then reject two incompatible typed inputs before
admission. No scientific submissions, client replay, key/config/capacity mutation,
or NVIDIA REST parity claim. Private receipts only; stdout contains IDs/statuses.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
from compatibility_fixtures import _validator, portable_payloads
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import MCPError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fs2_request_debug_live_verify", ROOT / "acceptance/request-debug-20260909/verify.py"
)
assert spec and spec.loader
debug = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = debug
spec.loader.exec_module(debug)
shared, check = debug.shared, debug.check
CALLS = ("phenoage", "qwen3-8b", "boltz2", "openfold2")
CONTROLS = {"wait_seconds", "idempotency_key"}


def fixtures():
    return {
        "phenoage": debug.customer.original_fixture(),
        "qwen3-8b": {
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 32,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        "boltz2": portable_payloads("boltz2")[0],
        "openfold2": portable_payloads("openfold2")[0],
    }


def invalid_fixtures():
    payloads = fixtures()
    boltz = deepcopy(payloads["boltz2"])
    del boltz["polymers"][0]["msa"]
    return {
        "boltz2": boltz,
        "openfold2": {**payloads["openfold2"], "alignments": {}},
    }


def local_refs(value):
    if isinstance(value, dict):
        if "$ref" in value:
            check(value["$ref"].startswith("#"), "external_schema_reference")
        for child in value.values():
            local_refs(child)
    elif isinstance(value, list):
        for child in value:
            local_refs(child)


def inspect_tool(tool):
    name, schema = tool["name"], tool["inputSchema"]
    description = tool.get("description", "").strip()
    check(len(description) >= 45 and description != name.replace("_", " "), "tool_description_not_meaningful")
    check(schema.get("type") == "object", "tool_schema_not_object")
    properties = schema.get("properties", {})
    check(set(properties) - CONTROLS, "opaque_tool_schema")
    check(not ({"payload", "request"} & set(properties)), "root_placeholder_in_schema")
    check(schema.get("additionalProperties") is False, "unbounded_root_schema")
    check(all(field.get("description", "").strip() for field in properties.values()), "field_description_missing")
    local_refs(schema)
    Draft202012Validator.check_schema(schema)


def inspect_contract(tool, contract):
    inspect_tool(tool)
    check(contract["tool_name"] == tool["name"], "schema_tool_name_mismatch")
    check(contract["input_schema"] == tool["inputSchema"], "schema_discovery_mismatch")
    check(contract.get("source_refs") and contract.get("model_ref"), "schema_provenance_missing")
    validator = Draft202012Validator(contract["input_schema"])
    for example in contract["examples"]:
        check(validator.is_valid(example), "published_example_invalid")
    return {
        "tool_name": tool["name"],
        "protocol": contract["protocol"],
        "model_ref": contract["model_ref"],
        "schema_sha256": shared.digest(contract["input_schema"]),
        "description_sha256": debug.sha(tool["description"].encode()),
        "fields": sorted(set(tool["inputSchema"]["properties"]) - CONTROLS),
        "example_count": len(contract["examples"]),
        "example_sha256": [shared.digest(item) for item in contract["examples"]],
        "examples_state": "validated" if contract["examples"] else "not_provided_asset_or_geometry_required",
        "source_refs": contract["source_refs"],
    }


async def tool_data(client, name, arguments):
    return shared._mcp_result(await client.call_tool(name, arguments))


async def inventory(client, output, secrets):
    tools, cursor = {}, None
    for _ in range(8):
        listing = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
        for tool in listing.tools:
            value = tool.model_dump(mode="json", by_alias=True)
            check(value["name"] not in tools, "duplicate_tool_name")
            tools[value["name"]] = value
        cursor = getattr(listing, "next_cursor", None)
        if not cursor:
            break
    check(not cursor, "tool_listing_exceeds_bound")
    debug.write(output / "raw/tools-list.json", list(tools.values()), secrets)
    serving = await tool_data(client, "list_models", {})
    science = await tool_data(client, "list_scientific_models", {})
    expected = {(row["id"], protocol) for row in serving["data"] for protocol in row["capabilities"]}
    expected |= {(row["model_id"], "scientific-batch-v1") for row in science["data"]}
    named = {}
    for name, tool in tools.items():
        meta = tool.get("_meta", tool.get("meta")) or {}
        if "fs2_model_id" not in meta:
            check(len((tool.get("description") or "").strip()) >= 45, "core_tool_description_missing")
            continue
        key = (meta["fs2_model_id"], meta["fs2_protocol"])
        check(key not in named and meta.get("fs2_input_contract") == "typed-model-v1", "model_tool_metadata_invalid")
        named[key] = name
    check(set(named) == expected, "authorized_model_tool_coverage_mismatch")
    check(set(CALLS) <= {model for model, _ in expected}, "required_smoke_models_unavailable")
    report, contracts = [], {}
    for model in sorted({model for model, _ in expected}):
        check(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", model), "model_identity_not_filename_safe")
        view = await tool_data(client, "get_model_schema", {"model_id": model})
        check(view["model_id"] == model, "schema_model_mismatch")
        debug.write(output / "raw" / ("schema-" + model + ".json"), view, secrets)
        seen = set()
        for contract in view["contracts"]:
            pair = (model, contract["protocol"])
            check(pair in named and pair not in seen, "unexpected_model_contract")
            seen.add(pair)
            tool = tools[named[pair]]
            report.append({"model_id": model, **inspect_contract(tool, contract)})
            contracts[pair] = contract
        check(seen == {pair for pair in expected if pair[0] == model}, "model_contract_missing_protocol")
    return report, contracts


def validate_result(model, payload, result):
    if model == "phenoage":
        shared.validate_result(model, 0, payload, result)
    elif model == "qwen3-8b":
        check(isinstance(result.get("choices"), list) and result["choices"], "qwen_choices_missing")
        content = result["choices"][0].get("message", {}).get("content")
        check(isinstance(content, str) and content.strip(), "qwen_useful_output_missing")
    elif model == "boltz2":
        polymer = payload["polymers"][0]
        _validator(model, "boltz2-native/validate_boltz2.py").validate_response(
            result, polymer["sequence"], polymer["id"]
        )
    else:
        _validator(model, "validate_openfold2.py").validate_response(result, payload["input_id"], payload["sequence"])


def expected_issue(model, error):
    check(error.code == -32602, "invalid_typed_call_not_invalid_params")
    check(isinstance(error.data, dict), "invalid_params_missing_data")
    data = error.data
    check(data.get("type") == "model_input_validation" and data.get("model_id") == model, "validation_identity")
    issues = data.get("issues", [])
    if model == "boltz2":
        check(any("msa" in issue.get("missing_fields", []) for issue in issues), "missing_msa_issue_not_actionable")
    else:
        check(
            any(issue.get("rule") == "additionalProperties" for issue in issues), "unsupported_alignment_issue_missing"
        )
    return {"code": error.code, "type": data["type"], "model_id": model, "issues": issues}


def exchange_events(cases):
    """Keep correlation and exact byte hashes even when a tool call fails."""
    return [
        {
            "tool": case["tool"],
            "model_id": case.get("model_id"),
            "operation_id": case.get("operation_id"),
            "probe": case["probe"],
            "started_at": case["started_at"],
            "endpoint": case["endpoint"],
            "http_status": case.get("status"),
            "request_sha256": debug.sha(case["request"]),
            "client_observed_response_sha256": debug.sha(case["response"]),
            "client_observed_response_bytes": len(case["response"]),
        }
        for case in cases
    ]


async def run_session(args, admin, recorder, saved, origin, canary, evidence, transport):
    async with shared.httpx2.AsyncClient(
        transport=transport,
        timeout=60,
        trust_env=False,
        headers={
            "authorization": "Bearer " + saved["secret"],
            "origin": origin,
            "accept-encoding": "identity",
            "x-api-key": canary,
        },
    ) as http:
        async with shared.Client(
            shared.streamable_http_client(origin + "/mcp", http_client=http), mode=shared.MCP_PROTOCOL_VERSION
        ) as client:
            report, contracts = await inventory(client, args.output, recorder.secrets)
            evidence["inventory"] = report
            debug.write(args.output / "inventory.json", report, recorder.secrets)
            apps = recorder.admin(admin, "/admin/api/v1/apps")
            check(not apps["next_cursor"], "apps_inventory_truncated")
            app_ids = {row["public_model_id"]: row["app_id"] for row in apps["items"]}
            for model, payload in fixtures().items():
                evidence["current_case"] = {"phase": "original_prediction", "model_id": model}
                protocol = "openai-chat" if model == "qwen3-8b" else "native"
                contract = contracts[(model, protocol)]
                arguments = payload | {"idempotency_key": "typed-live-" + str(uuid4()), "wait_seconds": 0}
                check(Draft202012Validator(contract["input_schema"]).is_valid(arguments), "positive_fixture_invalid")
                before = len(transport.cases)
                accepted = await tool_data(client, contract["tool_name"], arguments)
                check(len(transport.cases) == before + 1, "unexpected_model_submission_count")
                case = transport.cases[-1]
                case.update(name="typed-" + model, model_id=model, operation_id=accepted["id"])
                row = {
                    "model_id": model,
                    "tool_name": contract["tool_name"],
                    "operation_id": accepted["id"],
                    "payload_sha256": shared.digest(payload),
                    "started_at": case["started_at"],
                }
                evidence["operations"].append(row)
                debug.write(args.output / (model + "-accepted.json"), row, recorder.secrets)
                print(json.dumps({"model_id": model, "operation_id": accepted["id"], "state": "accepted"}), flush=True)
                end = time.monotonic() + args.timeout_seconds
                while time.monotonic() < end:
                    operation = await tool_data(client, "get_operation", {"operation_id": accepted["id"]})
                    if operation["status"] in debug.TERMINAL:
                        break
                    await asyncio.sleep(3)
                else:
                    raise debug.CheckError("typed_operation_timeout")
                check(operation["status"] == "succeeded", "typed_operation_not_succeeded")
                check(
                    operation["model_id"] == model and debug.owner_matches(operation, saved),
                    "operation_owner_model_mismatch",
                )
                result = await tool_data(client, "get_operation_result", {"operation_id": accepted["id"]})
                check(result["operation"]["id"] == accepted["id"], "result_operation_mismatch")
                debug.write(args.output / "raw" / (model + "-result.json"), result, recorder.secrets)
                validate_result(model, payload, result["result"])
                row.update(
                    outcome="passed",
                    completed_at=shared.now(),
                    terminal_status=operation["status"],
                    result_sha256=shared.digest(result["result"]),
                    runtime=operation.get("runtime"),
                    attempt=operation["attempt"],
                    accepted_at=operation["accepted_at"],
                    operation_completed_at=operation["completed_at"],
                    cold_start_seconds=operation.get("cold_start_seconds"),
                    estimated_gpu_seconds=operation.get("estimated_gpu_seconds"),
                )
                row["debug_capture"] = debug.verify_public_capture(
                    admin, recorder, case, saved, app_ids[model], args.capture_timeout_seconds
                )
            for model, payload in invalid_fixtures().items():
                evidence["current_case"] = {"phase": "preadmission_validation", "model_id": model}
                contract = contracts[(model, "native")]
                check(not Draft202012Validator(contract["input_schema"]).is_valid(payload), "negative_fixture_valid")
                before = len(transport.cases)
                try:
                    await client.call_tool(contract["tool_name"], payload)
                except MCPError as error:
                    issue = expected_issue(model, error)
                else:
                    raise debug.CheckError("invalid_typed_call_not_rejected")
                check(len(transport.cases) == before + 1, "unexpected_invalid_submission_count")
                case = transport.cases[-1]
                case.update(name="invalid-" + model, model_id=model)
                row = {
                    "model_id": model,
                    "tool_name": contract["tool_name"],
                    "outcome": "passed",
                    "error": issue,
                    "payload_sha256": shared.digest(payload),
                    "operation_id": None,
                }
                evidence["invalid_inputs"].append(row)
                row["debug_capture"] = debug.verify_public_capture(
                    admin, recorder, case, saved, app_ids[model], args.capture_timeout_seconds
                )
    evidence["mcp_session_closed"] = True
    evidence["mcp_tools_call_count"] = len(transport.cases)


def execute(args):
    check(re.fullmatch(r"[0-9a-f]{40}", args.release), "exact_release_required")
    check(not args.output.resolve().is_relative_to(ROOT.parent), "output_must_be_private")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.output / "raw").mkdir(mode=0o700)
    evidence = {
        "schema": "fs2-typed-mcp-acceptance/v1",
        "outcome": "failed",
        "release": args.release,
        "helper_sha256": debug.sha(Path(__file__).read_bytes()),
        "started_at": shared.now(),
        "operations": [],
        "invalid_inputs": [],
        "client_replays": 0,
        "key_mutations": 0,
        "settings_mutations": 0,
        "capacity_mutations": 0,
        "scientific_submissions": 0,
    }
    recorder, transport, secrets = None, None, ()
    try:
        saved = debug.read_key(args.key_file)
        access = json.loads(args.access_bundle.read_bytes())
        origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
        check(saved["origin"] == origin, "origin_mismatch")
        canary = "synthetic-redaction-check-" + str(uuid4())
        secrets = (saved["secret"], access["credentials"]["admin_bootstrap_token"], canary)
        recorder = debug.Recorder(args.output, secrets)
        with httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin:
            signed_in = False
            try:
                recorder.request(
                    admin, "POST", "/admin/api/v1/session", headers={"authorization": "Bearer " + secrets[1]}
                )
                signed_in = True
                keys = recorder.admin(admin, f"/admin/api/v1/users/{shared.owner_id(**saved['owner'])}/keys")["items"]
                key = next(row for row in keys if row["id"] == saved["key_id"])
                check(
                    key["state"] == "active" and set(key["scopes"]) <= set(debug.customer.SCOPES),
                    "ordinary_active_key_required",
                )
                evidence["owner"] = saved["owner"] | {"key_id": saved["key_id"]}
                evidence["current_case"] = {"phase": "catalog_and_schema_discovery"}
                transport = debug.MCPTransport()
                asyncio.run(run_session(args, admin, recorder, saved, origin, canary, evidence, transport))
                evidence["outcome"] = "passed"
                evidence.pop("current_case", None)
            finally:
                if signed_in:
                    recorder.request(admin, "DELETE", "/admin/api/v1/session", expected=(204,))
                    evidence["admin_logged_out"] = True
    except Exception as error:
        evidence.update(outcome="failed", error_type=type(error).__name__)
        if isinstance(error, debug.CheckError):
            evidence["error_code"] = str(error)
    finally:
        evidence["completed_at"] = shared.now()
        if recorder:
            debug.write(args.output / "admin-http-events.json", recorder.events, secrets)
        if transport:
            debug.write(args.output / "mcp-http-events.json", exchange_events(transport.cases), secrets)
        debug.write(args.output / "summary.json", evidence, secrets)
    print(json.dumps({"outcome": evidence["outcome"], "completed_at": evidence["completed_at"]}), flush=True)
    return int(evidence["outcome"] != "passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--access-bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--capture-timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    check(30 <= args.timeout_seconds <= 1800 and 5 <= args.capture_timeout_seconds <= 180, "timeouts_outside_bound")
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "offline-preparation",
                    "model_fixture_sha256": {model: shared.digest(payload) for model, payload in fixtures().items()},
                    "expected_original_operations": 4,
                    "expected_preadmission_errors": 2,
                    "scientific_submissions": 0,
                    "client_replays": 0,
                }
            )
        )
        return 0
    check(all((args.key_file, args.access_bundle, args.output, args.release)), "live_paths_required")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
