#!/usr/bin/env python3
"""Recover an existing ESMFold2 batch using reads only; never upload or submit.

The original failed attempt remains authoritative history. Recovered success
is supplemental terminal/artifact evidence, not a clean initial cohort pass.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from uuid import UUID

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from collect_live import check, now, private_json, write_private
from run_live import MCP_PROTOCOL_VERSION, ROOT, TERMINAL, batch_downloads, checkpoint, discovery, scientific_module


async def observe(args):
    key = private_json(args.key_file)
    check(key.get("disposable") is True and key["principal_id"].startswith("stockholm-canary-"),
          "disposable_canary_required")
    token = key["secret"]
    identifier = str(UUID(args.operation_id))
    args.output.mkdir(mode=0o700)
    report = {"operation_id": identifier, "started_at": now(), "scope": "read-only-existing-batch-recovery",
              "new_admissions": 0, "clean_initial_attempt": False, "customer_ready": False,
              "transport_failures": [], "states": []}
    module = scientific_module()
    config = module.RunConfig(endpoint=args.endpoint, repository_root=ROOT,
        activation_fragment=ROOT / "models/structure/batch-adapters/esmfold2/activation/public-acceptance.json",
        receipt_path=args.output / "scientific-receipt.json", run_id="read-only-existing-operation")
    model, request, declarations, fragment = module._activation(config)
    uploaded = private_json(args.upload_references)["artifacts"]
    check(len(uploaded) == 2, "two_known_upload_references_required")
    uploads = []
    for item in uploaded:
        pointer = {k: v for k, v in item.items() if k != "operation_id"}
        role = "request-input-manifest" if pointer["media_type"] == module.MANIFEST_MEDIA_TYPE else "manifest-artifact"
        if role == "manifest-artifact":
            match = [value for value in declarations if value.role == role and
                     hashlib.sha256(value.data).hexdigest() == pointer["sha256"]]
            check(len(match) == 1 and len(match[0].data) == pointer["size_bytes"], "uploaded_fixture_mismatch")
            pointer["name"] = match[0].name
        uploads.append(dict(pointer, role=role))
    input_pointer = next({k: v for k, v in row.items() if k != "role"}
                         for row in uploads if row["role"] == "request-input-manifest")
    async with httpx2.AsyncClient(base_url=args.endpoint, timeout=20, trust_env=False,
        follow_redirects=False, headers={"Authorization": "Bearer " + token, "Origin": args.endpoint}) as http:
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = await http.get(f"/v1/operations/{identifier}")
                check(response.status_code == 200, "existing_operation_read_failed")
                status = response.json()
            except httpx2.TransportError as error:
                report["transport_failures"].append({"at": now(), "type": type(error).__name__})
                check(len(report["transport_failures"]) <= 5, "bounded_transport_retries_exhausted")
                checkpoint(args.output / "progress.json", report, token)
                await asyncio.sleep(5)
                continue
            module._operation_identity(status, operation_id=identifier, model_id=model, operation=request["operation"])
            check(status["operation"]["token_id"] == key["token_id"], "operation_canary_mismatch")
            state = {"operation": status["operation"]["status"], "batch": status["batch"]["status"]}
            if not report["states"] or report["states"][-1]["state"] != state:
                report["states"].append({"at": now(), "state": state})
                checkpoint(args.output / "progress.json", report, token)
                print(json.dumps(state), flush=True)
            if state["operation"] in TERMINAL or state["batch"] in TERMINAL:
                check(state == {"operation": "succeeded", "batch": "succeeded"}, "existing_batch_terminal_failure")
                if status["batch"]["result_published"]:
                    break
            await asyncio.sleep(5)
        else:
            raise TimeoutError("existing_batch_wait_timeout")
        response = await http.get(f"/v1/operations/{identifier}/result")
        check(response.status_code == 200, "existing_batch_result_unavailable")
        result = response.json()
        module._validate_result(result, status=status, operation_id=identifier, model_id=model,
                                input_pointer=input_pointer, fragment=fragment)
        receipt = module._receipt(client=module.PublicApiClient(args.endpoint, token), model_id=model,
                                  status=status, result=result, uploads=uploads)
        write_private(args.output / "scientific-receipt.json", receipt, (token,))
        async with Client(streamable_http_client(args.endpoint + "/mcp", http_client=http),
                          mode=MCP_PROTOCOL_VERSION) as client:
            found, _ = await discovery(client)
            report["verified_batch_downloads"] = await batch_downloads(client, receipt, found["tools"])
        report.update(completed_at=now(), outcome="existing_batch_terminal_and_artifacts_passed")
        write_private(args.output / "completed.json", report, (token,))
        print(json.dumps({"outcome": report["outcome"], "operation_id": identifier, "output": str(args.output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--upload-references", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    args = parser.parse_args()
    check(args.endpoint.startswith("https://") and 0 < args.timeout_seconds <= 1800, "bounded_https_read_required")
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(observe(args))
    except Exception as error:
        print(json.dumps({"outcome": "recovery_incomplete", "failure_type": type(error).__name__,
                          "code": getattr(error, "code", None)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
