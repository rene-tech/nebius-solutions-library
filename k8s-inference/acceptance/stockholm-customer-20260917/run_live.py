#!/usr/bin/env python3
"""Bounded, resumable real Stockholm SDK/HTTP probes; never a LibreChat receipt.

Default is an offline plan. --execute additionally requires an already-issued,
owner-only disposable canary file and an exact running CP digest. No credentials
are created, rotated or revoked by this runner. Existing customer keys are refused.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from builtins import BaseExceptionGroup
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from collect_live import (
    check, collect, digest, now, policy, private_json, release_facts,
    require_release, write_private,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "components/control-plane/src"))
sys.path.insert(0, str(ROOT / "acceptance/mcp-model-contracts-20260909"))
from compatibility_fixtures import _validator, portable_payloads  # noqa: E402
from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result  # noqa: E402

MODELS = ("openfold2", "boltz2")
# Warm OpenFold2 can finish before a shared MCP session admits five requests.
# Admit the existing longer Boltz2 fixtures first, then the fast model last;
# still require five real overlapping server lifetimes, never infer from tasks.
MIXED_MODELS = ("boltz2", "boltz2", "boltz2", "boltz2", "openfold2")
SCENARIOS = ("named-sdk", "generic-sdk", "legacy-generic-sdk", "public-api")
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "preempted"}


def offline_plan() -> dict:
    return {
        "scope": "partial-stockholm-sdk-http-and-representative-batch",
        "models": list(MODELS), "serving_scenarios": list(SCENARIOS),
        "cohorts": 2, "mixed_parallel_submissions": 5,
        "mixed_submission_order": list(MIXED_MODELS),
        "serving_operations_per_cohort": 13,
        "batch": "esmfold2 public upload/queue/terminal/artifact acceptance, one per cohort",
        "fixture_sha256": {model: [digest(value) for value in portable_payloads(model)] for model in MODELS},
        "requires": ["new CP rollout explicitly approved by parent", "immutable deployed CP digest",
                     "new disposable same-policy Stockholm canary (0600 key file)",
                     "no pre-existing unfinished operations on that canary"],
        "not_proven": ["actual LibreChat/installed-skill execution", "all advertised Apps",
                       "warning/restart/resource-leak/usage reconciliation", "full release identity"],
        "customer_ready": False,
    }


def validate_canary(key: dict, snapshot: dict) -> str:
    check(key.get("schema") == "fs2-customer-key/v1" and key.get("disposable") is True,
          "disposable_key_required")
    token = key.get("secret")
    check(isinstance(token, str) and bool(token), "canary_secret_missing")
    import hashlib
    from datetime import UTC, datetime

    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    rows = [row for row in snapshot["token_metadata"] if row.get("fingerprint") == fingerprint]
    check(len(rows) == 1, "canary_not_in_live_stockholm_policy")
    row = rows[0]
    check(row.get("principal_id", "").startswith("stockholm-canary-")
          and row.get("name", "").startswith("stockholm-canary-")
          and row.get("revoked_at") is None, "real_customer_key_refused")
    check(row.get("expires_at") is not None and datetime.fromisoformat(
        row["expires_at"].replace("Z", "+00:00")) > datetime.now(UTC), "canary_expired_or_unbounded")
    check(policy(row) == snapshot["team_policy"], "canary_team_policy_mismatch")
    check(key.get("token_id") == row["id"] and key.get("principal_id") == row["principal_id"],
          "canary_identity_mismatch")
    return token


def checkpoint(path: Path, value: object, token: str) -> None:
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".pending")
    write_private(temporary, value, (token,))
    os.replace(temporary, path)


def failures(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in failures(child)]
    return [{"type": type(error).__name__, "code": getattr(error, "code", None)}]


def arguments(model: str, scenario: str, payload: dict, idempotency: str, operation: str | None = None) -> dict:
    controls = {"idempotency_key": idempotency, "wait_seconds": 0}
    if scenario == "named-sdk":
        return deepcopy(payload) | controls
    if scenario == "generic-sdk":
        return {"model_id": model, "protocol": "native", "payload": deepcopy(payload), **controls}
    if scenario == "legacy-generic-sdk":
        return {"model_id": model, "protocol": "native", "payload": deepcopy(payload) | controls}
    check(scenario == "public-api", "unknown_scenario")
    # Dynamic portable routes can differ from the base catalog (OpenFold2's
    # live operation is predict-structure, not the base NIM's predict).
    check(isinstance(operation, str) and operation, "discovered_native_operation_required")
    return {"operation": operation, "payload": deepcopy(payload)}


def semantic(model: str, payload: dict, result: dict) -> None:
    if model == "boltz2":
        polymer = payload["polymers"][0]
        _validator(model, "boltz2-native/validate_boltz2.py").validate_response(
            result, polymer["sequence"], polymer["id"])
    else:
        _validator(model, "validate_openfold2.py").validate_response(
            result, payload["input_id"], payload["sequence"])


def concurrent_peak(records: list[dict]) -> int:
    # Use server lifecycle times, not submission tasks or slow poll completion.
    events = []
    for row in records:
        op = row.get("terminal_operation", {})
        if op.get("accepted_at") and op.get("completed_at"):
            events.extend([(op["accepted_at"], 1), (op["completed_at"], -1)])
    current = peak = 0
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


async def discovery(client: Client) -> tuple[dict, dict]:
    tools, cursor = {}, None
    for _ in range(16):
        page = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
        for tool in page.tools:
            check(tool.name not in tools, "duplicate_tool_name")
            tools[tool.name] = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    check(not cursor, "tool_inventory_truncated")
    serving = _mcp_result(await client.call_tool("list_models", {}))
    batch = _mcp_result(await client.call_tool("list_scientific_models", {}))
    contracts = {}
    for model in MODELS:
        schema = _mcp_result(await client.call_tool("get_model_schema", {"model_id": model, "protocol": "native"}))
        contract = next(row for row in schema["contracts"] if row["protocol"] == "native")
        check(tools[contract["tool_name"]]["inputSchema"] == contract["input_schema"], "discovery_schema_drift")
        live_model = next(row for row in serving["data"] if row["id"] == model)
        check(len(live_model["operations"]) == 1, "ambiguous_native_operation")
        contract["operation"] = live_model["operations"][0]
        contracts[model] = contract
    return {"tools": tools, "serving": serving, "scientific": batch,
            "tool_catalog_sha256": digest([tools[name] for name in sorted(tools)])}, contracts


async def case(client, http, contract, model, scenario, payload, path, identity, token, timeout, stop_file=None):
    if path.exists():
        row = private_json(path)
        check(row["run_identity"] == identity, "resume_identity_mismatch")
        check(row["model_id"] == model and row["scenario"] == scenario
              and row["fixture_sha256"] == digest(payload), "resume_case_mismatch")
    else:
        row = {"run_identity": identity, "model_id": model, "scenario": scenario,
               "fixture_sha256": digest(payload), "idempotency_key": "stockholm-" + str(uuid4()),
               "client_case_id": str(uuid4()), "started_at": now(), "state": "prepared"}
        row["arguments"] = arguments(model, scenario, payload, row["idempotency_key"], contract["operation"])
        # Persist the exact request before any potentially durable submission.
        checkpoint(path, row, token)
    if row["state"] == "passed":
        return row
    args = row["arguments"]
    Draft202012Validator(contract["input_schema"]).validate(
        deepcopy(payload) | {"idempotency_key": row["idempotency_key"], "wait_seconds": 0})
    tool = contract["tool_name"] if scenario == "named-sdk" else "invoke_model"

    async def submit():
        if scenario == "public-api":
            response = await http.post(f"/v1/models/{model}:invoke", json=args, headers={
                "Idempotency-Key": row["idempotency_key"], "X-FS2-Wait-Seconds": "0",
                "X-Request-ID": row["client_case_id"],
            })
            row["last_http_status"] = response.status_code
            row["last_http_response_sha256"] = "sha256:" + __import__("hashlib").sha256(response.content).hexdigest()
            check(response.status_code in {200, 202}, "http_admission_failed")
            return str(UUID(response.headers["x-fs2-operation-id"]))
        accepted = _mcp_result(await client.call_tool(tool, args))
        return str(UUID(accepted["id"]))

    try:
        if not row.get("operation_id"):
            check(stop_file is None or not stop_file.exists(), "operator_paused_new_submissions")
            row["state"] = "submitting"
            checkpoint(path, row, token)
            row["operation_id"] = await submit()
            row["state"] = "accepted"
            checkpoint(path, row, token)
        op_id = row["operation_id"]
        # Replay while in flight: admission cannot charge another operation.
        row["replay_operation_id"] = await submit()
        check(row["replay_operation_id"] == op_id, "idempotency_replay_changed_operation")
        checkpoint(path, row, token)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await http.get(f"/v1/operations/{op_id}")
            check(response.status_code == 200, "operation_read_failed")
            operation = response.json()
            row["last_operation"] = operation
            statuses = row.setdefault("observed_statuses", [])
            if not statuses or statuses[-1]["status"] != operation["status"]:
                statuses.append({"at": now(), "status": operation["status"]})
                checkpoint(path, row, token)
            if operation["status"] in TERMINAL:
                row["terminal_operation"] = operation
                check(operation["status"] == "succeeded", "operation_terminal_failure")
                break
            await asyncio.sleep(3)
        else:
            raise TimeoutError("operation_timeout")
        if scenario == "public-api":
            response = await http.get(f"/v1/operations/{op_id}/result")
            check(response.status_code == 200, "http_result_failed")
            result = response.json()
        else:
            envelope = _mcp_result(await client.call_tool("get_operation_result", {"operation_id": op_id}))
            check(envelope["operation"]["id"] == op_id, "mcp_result_identity_mismatch")
            result = envelope["result"]
        semantic(model, payload, result)
        row.update(result_sha256=digest(result), semantic_validated=True,
                   terminal_replay_operation_id=await submit(), state="passed", completed_at=now())
        check(row["terminal_replay_operation_id"] == op_id, "terminal_replay_changed_operation")
        result_path = path.with_name(path.stem + "-result.json")
        if not result_path.exists():
            write_private(result_path, result, (token,))
    except Exception as error:
        # Exceptions may contain raw client payloads/headers; retain type only.
        row.update(state="failed", failure_type=type(error).__name__, failures=failures(error), checked_at=now())
        checkpoint(path, row, token)
        raise
    checkpoint(path, row, token)
    print(json.dumps({"model_id": model, "scenario": scenario, "operation_id": row["operation_id"],
                      "status": row["state"]}), flush=True)
    return row


def scientific_module():
    spec = importlib.util.spec_from_file_location(
        "stockholm_scientific_public_runner", ROOT / "acceptance/scientific-fleet/run_acceptance.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_batch(args, token, origin, cohort):
    module = scientific_module()
    path = args.output / f"cohort-{cohort}-esmfold2-batch.json"
    if path.exists():
        return private_json(path)
    config = module.RunConfig(
        endpoint=origin, repository_root=ROOT,
        activation_fragment=ROOT / "models/structure/batch-adapters/esmfold2/activation/public-acceptance.json",
        receipt_path=path, run_id=f"stockholm-{args.run_id}-{cohort}",
        timeout_seconds=args.timeout_seconds,
    )
    # Existing runner verifies scientific semantics and artifact references.
    # The advertised MCP download handle + HTTPS verifies the downloaded bytes.
    return module.run_acceptance(config, module.PublicApiClient(origin, token))


def require_batch_download_tool(tools):
    check("download_scientific_artifact" in tools, "batch_download_tool_not_advertised")


async def batch_downloads(client, receipt, tools, download_http=None):
    import hashlib
    from datetime import UTC, datetime
    from urllib.parse import urlsplit

    require_batch_download_tool(tools)
    measured = []

    async def read(reference, transport):
        check(0 < reference["size_bytes"] <= 1024 * 1024, "batch_artifact_exceeds_inline_bound")
        identifier = str(UUID(reference["artifact_id"]))
        result = _mcp_result(await client.call_tool("download_scientific_artifact", {"artifact_id": identifier}))
        actual = result["artifact"]
        check(all(actual[field] == reference[field] for field in ("artifact_id", "size_bytes", "sha256")),
              "batch_download_reference_mismatch")
        handle = result["handle"]
        address = urlsplit(handle["url"])
        check(handle["method"] == "GET" and address.scheme == "https" and address.hostname
              and not address.username and not address.password, "batch_download_handle_invalid")
        check(datetime.fromisoformat(handle["expires_at"].replace("Z", "+00:00")) > datetime.now(UTC),
              "batch_download_handle_expired")
        # Separate unauthenticated client: never forward the gateway bearer to
        # object storage. Signed URLs/headers remain transient, outside receipts.
        content = bytearray()
        async with transport.stream("GET", handle["url"], headers=handle["headers"]) as response:
            check(response.status_code == 200, "batch_download_http_failed")
            async for chunk in response.aiter_raw():
                content.extend(chunk)
                check(len(content) <= reference["size_bytes"], "batch_artifact_bytes_oversized")
        check(len(content) == reference["size_bytes"]
              and hashlib.sha256(content).hexdigest() == reference["sha256"], "batch_artifact_bytes_mismatch")
        measured.append({"artifact_id": identifier, "size_bytes": len(content), "sha256": reference["sha256"]})
        return content

    async def read_all(transport):
        manifest = json.loads(await read(receipt["artifact_digests"]["output_manifest"], transport))
        check(isinstance(manifest.get("entries"), list) and manifest["entries"], "batch_output_manifest_empty")
        for entry in manifest["entries"]:
            await read(entry["artifact"], transport)

    if download_http is not None:
        await read_all(download_http)
    else:
        async with httpx2.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as transport:
            await read_all(transport)
    return measured


async def execute(args) -> int:
    check(args.key_file and args.kubeconfig and args.context and args.expected_cp_image,
          "live_identity_arguments_missing")
    before = await asyncio.to_thread(collect, args.kubeconfig, args.context)
    require_release(before, args.expected_cp_image)
    key = private_json(args.key_file)
    token = validate_canary(key, before)
    origin = before["public_endpoint"].rstrip("/")
    check(origin.startswith("https://"), "https_required")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    identity = digest({"release": release_facts(before), "token_id": key["token_id"]})
    state_path = args.output / "run.json"
    if args.resume:
        saved = private_json(state_path)
        check(saved["run_identity"] == identity, "resume_release_or_key_changed")
        args.run_id = saved["run_id"]
        previous_receipt = args.output / "partial-receipt.json"
        if previous_receipt.exists():
            write_private(args.output / ("previous-attempt-" + str(uuid4()) + ".json"),
                          private_json(previous_receipt), (token,))
    else:
        args.run_id = str(uuid4())
        write_private(state_path, {"run_identity": identity, "run_id": args.run_id}, (token,))
    stamp = str(uuid4())
    write_private(args.output / f"snapshot-before-{stamp}.json", before, (token,))
    receipt = {"scope": offline_plan()["scope"], "run_identity": identity,
               "started_at": now(), "customer_ready": False, "resumed": args.resume,
               "cohorts": [], "outcome": "failed"}
    try:
        async with httpx2.AsyncClient(base_url=origin, timeout=60, trust_env=False, follow_redirects=False,
                                     headers={"Authorization": "Bearer " + token, "Origin": origin}) as http:
            async with Client(streamable_http_client(origin + "/mcp", http_client=http),
                              mode=MCP_PROTOCOL_VERSION) as client:
                for cohort in range(1, args.cohorts + 1):
                    found, contracts = await discovery(client)
                    if args.batch:
                        require_batch_download_tool(found["tools"])
                    checkpoint(args.output / f"cohort-{cohort}-discovery.json", found, token)
                    record = {"sequence": cohort, "tool_catalog_sha256": found["tool_catalog_sha256"],
                              "calls": [], "coverage": "OpenFold2/Boltz2 only; not all advertised Apps"}
                    receipt["cohorts"].append(record)
                    for model in MODELS:
                        for scenario in SCENARIOS:
                            row = await case(client, http, contracts[model], model, scenario,
                                portable_payloads(model)[cohort % 2],
                                args.output / f"cohort-{cohort}-{model}-{scenario}.json",
                                identity, token, args.timeout_seconds, args.stop_file)
                            record["calls"].append(row)
                    tasks = []
                    for index, model in enumerate(MIXED_MODELS):
                        tasks.append(case(client, http, contracts[model], model, "named-sdk",
                            portable_payloads(model)[index % 2],
                            args.output / f"cohort-{cohort}-mixed-{index}-{model}.json",
                            identity, token, args.timeout_seconds, args.stop_file))
                    # Wait for every already-started task even when one fails;
                    # never orphan the other admitted cohort operations.
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    rows = [row for row in results if isinstance(row, dict)]
                    record["mixed_calls"] = rows
                    record["observed_peak_outstanding"] = concurrent_peak(rows)
                    check(len(rows) == 5, "mixed_cohort_failed")
                    check(record["observed_peak_outstanding"] == 5, "concurrency_five_not_observed")
                    if args.batch:
                        batch_exists = (args.output / f"cohort-{cohort}-esmfold2-batch.json").exists()
                        check(batch_exists or args.stop_file is None or not args.stop_file.exists(),
                              "operator_paused_new_submissions")
                        record["batch"] = await asyncio.to_thread(run_batch, args, token, origin, cohort)
                        record["verified_batch_downloads"] = await batch_downloads(
                            client, record["batch"], found["tools"])
                    refreshed, _ = await discovery(client)
                    check(found["tool_catalog_sha256"] == refreshed["tool_catalog_sha256"], "tool_catalog_drift")
                    after_cohort = await asyncio.to_thread(collect, args.kubeconfig, args.context)
                    require_release(after_cohort, args.expected_cp_image)
                    check(release_facts(before) == release_facts(after_cohort), "release_changed_during_cohort")
                    write_private(args.output / f"snapshot-after-{cohort}-{stamp}.json", after_cohort, (token,))
                    checkpoint(args.output / "partial-receipt.json", receipt, token)
        receipt["outcome"] = "partial_scope_passed"
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
        receipt["failures"] = failures(error)
    receipt["completed_at"] = now()
    checkpoint(args.output / "partial-receipt.json", receipt, token)
    print(json.dumps({"outcome": receipt["outcome"], "output": str(args.output), "customer_ready": False}))
    return 0 if receipt["outcome"] == "partial_scope_passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--expected-cp-image")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cohorts", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-file", type=Path, help="If present, stop new admissions but finish already-known operations")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(offline_plan(), indent=2))
        return 0
    check(args.output and 0 < args.timeout_seconds <= 7200, "bounded_execution_arguments_required")
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
