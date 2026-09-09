#!/usr/bin/env python3
"""Bounded request-debug acceptance. Offline preparation unless --execute.

Use only after the release manager's live GO. Three sequential native operations:
one existing synthetic PhenoAge success and two deliberately invalid empty
Boltz2/OpenFold2 payloads. Additional malformed HTTP/MCP requests must not admit
work. No replay, key/tenant/settings/capacity changes or scientific submissions.
Raw captures stay in the fresh private output; stdout contains status only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import stat
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from mcp.shared.exceptions import MCPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/customer-access-20260908"))
import customer_access as customer  # noqa: E402

shared = customer.shared
MODELS = {"phenoage", "boltz2", "openfold2"}
CASES = ("phenoage", "boltz2", "openfold2", "malformed-http", "mcp")
DETAIL_FIELDS = {"query_string", "request_headers", "response_headers", "request_body", "response_body", "error_detail"}
PROBE_HEADER = "x-fs2-debug-probe"
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "preempted"}


class CheckError(RuntimeError):
    """Fixed code only: exception strings may contain private response bodies."""


def check(value, code):
    if not value:
        raise CheckError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, "key_file_requires_0600")
        saved = json.load(stream)
    check(saved.get("schema") == "fs2-customer-key/v1", "key_schema")
    check(set(saved.get("owner", {})) == {"tenant_id", "principal_id"}, "key_owner_missing")
    identity, _ = shared.TokenService._parse(saved["secret"])
    check(str(identity) == saved["key_id"], "key_identity_mismatch")
    return saved


def no_secrets(value, secrets):
    serialized = json.dumps(value, ensure_ascii=False)
    check(all(secret not in serialized for secret in secrets if secret), "capture_contains_credential")
    check(not re.search(r"fs2_(?:pat|admin)_[A-Za-z0-9_-]{16,}", serialized), "capture_contains_key_material")


def write(path, value, secrets=()):
    no_secrets(value, secrets)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def body_bytes(body):
    check(body["encoding"] in {"utf-8", "base64"}, "capture_encoding")
    return body["data"].encode() if body["encoding"] == "utf-8" else base64.b64decode(body["data"], validate=True)


def exact_body(body, expected):
    check(body["complete"] and not body["redacted"], "synthetic_body_capture_incomplete_or_changed")
    check(body_bytes(body) == expected, "captured_body_bytes_differ")
    check(body["observed_bytes"] == len(expected), "captured_body_count_differs")


def owner_matches(value, saved):
    return (
        all(value.get(key) == expected for key, expected in saved["owner"].items())
        and value.get("token_id") == saved["key_id"]
    )


def advertised_operation(discovery, model_id):
    model = next(row for row in discovery["data"] if row["id"] == model_id)
    operations = model.get("operations", [])
    check(len(operations) == 1 and isinstance(operations[0], str), "native_operation_ambiguous")
    return operations[0]


def json_rpc_messages(raw):
    try:
        return [json.loads(raw)]
    except ValueError:
        return [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith(b"data:")]


def rpc_failed(raw):
    return any("error" in item or item.get("result", {}).get("isError") is True for item in json_rpc_messages(raw))


class Recorder:
    def __init__(self, output, secrets):
        self.output, self.secrets, self.events = output, secrets, []

    def request(self, client, method, path, *, content=None, headers=None, expected=(200,)):
        started, clock = shared.now(), time.monotonic()
        event = {"at": started, "method": method, "path": path.split("?", 1)[0]}
        try:
            response = client.request(method, path, content=content, headers=headers)
            event.update(
                status=response.status_code, response_bytes=len(response.content), response_sha256=sha(response.content)
            )
            check(response.status_code in expected, f"unexpected_http_{response.status_code}")
            return response
        except Exception as error:
            event["error_type"] = type(error).__name__
            raise
        finally:
            event.update(completed_at=shared.now(), seconds=time.monotonic() - clock)
            self.events.append(event)

    def admin(self, client, path):
        return self.request(client, "GET", path).json()["data"]


def terminal(client, recorder, operation_id, timeout):
    deadline, observations = time.monotonic() + timeout, []
    while time.monotonic() < deadline:
        value = recorder.request(client, "GET", f"/v1/operations/{operation_id}").json()
        observations.append({"at": shared.now(), "status": value["status"]})
        if value["status"] in TERMINAL:
            return value, observations
        time.sleep(3)
    raise CheckError("operation_completion_timeout")


def capture_list(admin, recorder, path, started_at, operation_id=None):
    params = {"from": started_at, "to": shared.now(), "limit": "200"}
    if operation_id:
        params["operation_id"] = operation_id
    rows = []
    for _ in range(5):
        query = str(httpx.QueryParams(params))
        listing = recorder.admin(admin, path + "?" + query)
        check(all(not (DETAIL_FIELDS & set(item)) for item in listing["items"]), "summary_contains_payload")
        rows.extend(listing["items"])
        if not listing["next_cursor"]:
            return rows
        params["cursor"] = listing["next_cursor"]
    raise CheckError("capture_inventory_exceeds_bound")


def verify_public_capture(admin, recorder, case, saved, app_id, timeout):
    path = f"/admin/api/v1/apps/{app_id}/requests" if app_id else "/admin/api/v1/requests"
    deadline, inspected = time.monotonic() + timeout, set()
    while time.monotonic() < deadline:
        rows = capture_list(admin, recorder, path, case["started_at"], case.get("operation_id"))
        for row in rows:
            if row["id"] in inspected or row["source"] != "public" or row["method"] != "POST":
                continue
            if not owner_matches(row, saved) or row["endpoint"] != case["endpoint"]:
                continue
            if row.get("mcp_tool") != case.get("tool"):
                continue
            detail = recorder.admin(admin, path + "/" + row["id"])
            inspected.add(row["id"])
            headers = {key.lower(): value for key, value in detail["request_headers"]}
            if headers.get(PROBE_HEADER) != case["probe"]:
                continue
            # Preserve a genuine mismatch too, but never write a leaked credential.
            write(recorder.output / "raw" / f"{case['name']}-public.json", detail, recorder.secrets)
            check(owner_matches(detail, saved), "capture_owner_mismatch")
            check(detail["operation_id"] == case.get("operation_id"), "capture_operation_correlation")
            check(detail["request_id"] == row["request_id"] and detail["request_id"], "capture_request_id_missing")
            UUID(detail["request_id"])
            check(detail["http_status"] == case["status"], "capture_http_status_mismatch")
            check(detail["model_id"] == case.get("model_id"), "capture_model_mismatch")
            check(not detail["disconnected"], "capture_disconnected")
            for direction in ("request", "response"):
                check(
                    detail[direction + "_body"]["content_type"] == case[direction + "_content_type"],
                    "capture_content_type_mismatch",
                )
            check(headers.get("authorization") == "[REDACTED]", "authorization_not_removed")
            check(headers.get("x-api-key") == "[REDACTED]", "synthetic_credential_header_not_removed")
            no_secrets(detail, recorder.secrets)
            exact_body(detail["request_body"], case["request"])
            exact_body(detail["response_body"], case["response"])
            # Both authorized routes must return the same exact retained document.
            check(recorder.admin(admin, "/admin/api/v1/requests/" + row["id"]) == detail, "global_app_detail_mismatch")
            return {
                "exchange_id": row["id"],
                "request_id": row["request_id"],
                "http_status": detail["http_status"],
                "request_sha256": sha(case["request"]),
                "response_sha256": sha(case["response"]),
                "request_bytes": len(case["request"]),
                "response_bytes": len(case["response"]),
                "owner_verified": True,
                "correlation_verified": True,
                "auth_redacted": True,
                "complete_exact_body_capture": True,
            }
        time.sleep(2)
    raise CheckError("public_capture_persistence_timeout")


def verify_upstream(admin, recorder, case, saved, app_id, payload, timeout, expected_statuses):
    path = f"/admin/api/v1/apps/{app_id}/requests"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = capture_list(admin, recorder, path, case["started_at"], case["operation_id"])
        upstream = [row for row in rows if row["source"] == "upstream"]
        if upstream:
            verified = []
            for index, row in enumerate(upstream):
                detail = recorder.admin(admin, path + "/" + row["id"])
                write(recorder.output / "raw" / f"{case['name']}-upstream-{index}.json", detail, recorder.secrets)
                check(owner_matches(detail, saved), "upstream_owner_mismatch")
                check(
                    detail["operation_id"] == case["operation_id"] and detail["model_id"] == case["model_id"],
                    "upstream_identity_mismatch",
                )
                check(detail["http_status"] in expected_statuses, "upstream_validation_status_missing")
                check(
                    detail["operation_attempt"] is not None and detail["upstream_attempt"] is not None,
                    "upstream_attempt_missing",
                )
                exact_body(detail["request_body"], shared.encoded(payload))
                response = body_bytes(detail["response_body"])
                exact_body(detail["response_body"], response)
                check(response, "upstream_response_body_empty")
                if detail["http_status"] >= 400:
                    check(detail["error_type"] is not None, "upstream_error_not_recorded")
                else:
                    shared.validate_result("phenoage", 0, payload, json.loads(response))
                no_secrets(detail, recorder.secrets)
                check(
                    recorder.admin(admin, "/admin/api/v1/requests/" + row["id"]) == detail,
                    "upstream_global_detail_mismatch",
                )
                verified.append(
                    {
                        "exchange_id": row["id"],
                        "http_status": detail["http_status"],
                        "operation_attempt": detail["operation_attempt"],
                        "upstream_attempt": detail["upstream_attempt"],
                        "request_sha256": sha(body_bytes(detail["request_body"])),
                        "response_sha256": sha(response),
                        "response_bytes": len(response),
                        "complete": True,
                        "owner_verified": True,
                    }
                )
            return verified
        time.sleep(2)
    raise CheckError("upstream_capture_persistence_timeout")


class WireStream(shared.httpx2.AsyncByteStream):
    def __init__(self, original, case):
        self.original, self.case = original, case

    async def __aiter__(self):
        async for chunk in self.original:
            self.case["response"] += chunk
            yield chunk

    async def aclose(self):
        await self.original.aclose()


class MCPTransport(shared.httpx2.AsyncBaseTransport):
    """Observe the same SDK stream as consumed; no extra call or body drain."""

    def __init__(self):
        self.inner = shared.httpx2.AsyncHTTPTransport()
        self.cases = []

    async def handle_async_request(self, request):
        try:
            body = json.loads(request.content)
        except (ValueError, shared.httpx2.RequestNotRead):
            body = {}
        case = None
        if isinstance(body, dict) and body.get("method") == "tools/call":
            tool = body["params"]["name"]
            probe = str(uuid4())
            request.headers[PROBE_HEADER] = probe
            case = {
                "name": "mcp-" + tool,
                "probe": probe,
                "tool": tool,
                "model_id": body["params"].get("arguments", {}).get("model_id"),
                "started_at": shared.now(),
                "endpoint": request.url.path,
                "request": request.content,
                "request_content_type": request.headers.get("content-type"),
                "response": b"",
            }
            self.cases.append(case)
        response = await self.inner.handle_async_request(request)
        if case is not None:
            case["status"] = response.status_code
            case["response_content_type"] = response.headers.get("content-type")
            response.stream = WireStream(response.stream, case)
        return response

    async def aclose(self):
        await self.inner.aclose()


async def mcp_cases(origin, saved, canary):
    transport = MCPTransport()
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
            result = shared._mcp_result(await client.call_tool("list_models", {}))
            check(MODELS <= {row["id"] for row in result["data"]}, "mcp_models_missing")
            try:
                await client.call_tool(
                    "invoke_model", {"model_id": "phenoage", "protocol": "native", "payload": "invalid-object"}
                )
            except MCPError:
                pass  # Exact server response below, not exception alone, establishes denial.
    check(len(transport.cases) == 2, "mcp_actual_tool_call_count")
    check(not rpc_failed(transport.cases[0]["response"]), "mcp_discovery_error")
    check(rpc_failed(transport.cases[1]["response"]), "mcp_malformed_arguments_not_rejected")
    return transport.cases


def execute(args):
    check(re.fullmatch(r"[0-9a-f]{40}", args.release), "exact_deployed_release_required")
    check(not args.output.resolve().is_relative_to(ROOT.parent), "output_must_be_private_outside_repository")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.output / "raw").mkdir(mode=0o700)
    evidence = {
        "schema": "fs2-request-debug-acceptance/v1",
        "outcome": "failed",
        "release": args.release,
        "started_at": shared.now(),
        "helper_sha256": sha(Path(__file__).read_bytes()),
        "cases": [],
        "key_mutations": 0,
        "settings_mutations": 0,
        "capacity_mutations": 0,
        "client_replays": 0,
        "selected_cases": args.cases,
    }
    secrets, recorder = (), None
    try:
        saved = read_key(args.key_file)
        access = json.loads(args.access_bundle.read_bytes())
        origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
        check(origin == saved["origin"], "private_origin_mismatch")
        canary = "synthetic-redaction-check-" + str(uuid4())
        secrets = (saved["secret"], access["credentials"]["admin_bootstrap_token"], canary)
        recorder = Recorder(args.output, secrets)
        with (
            httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin,
            customer.public_client(origin, saved["secret"]) as public,
        ):
            signed_in = False
            try:
                recorder.request(
                    admin, "POST", "/admin/api/v1/session", headers={"authorization": "Bearer " + secrets[1]}
                )
                signed_in = True
                owner_id = shared.owner_id(**saved["owner"])
                keys = recorder.admin(admin, f"/admin/api/v1/users/{owner_id}/keys")["items"]
                key = next(row for row in keys if row["id"] == saved["key_id"])
                check(
                    all(key[field] == value for field, value in saved["owner"].items()) and key["state"] == "active",
                    "retained_owner_identity",
                )
                check(
                    set(key["scopes"]) <= set(customer.SCOPES)
                    and {"inference.invoke", "mcp.invoke", "catalog.read", "operations.read", "operations.result"}
                    <= set(key["scopes"]),
                    "ordinary_scope_required",
                )
                apps = recorder.admin(admin, "/admin/api/v1/apps")
                check(not apps.get("next_cursor"), "app_inventory_truncated")
                app_ids = {row["public_model_id"]: row["app_id"] for row in apps["items"]}
                check(MODELS <= set(app_ids), "required_apps_missing")
                discovery = recorder.request(public, "GET", "/v1/models").json()
                check(MODELS <= {row["id"] for row in discovery["data"]}, "ordinary_models_missing")
                evidence["owner"] = {**saved["owner"], "key_id": saved["key_id"]}
                for model in ("phenoage", "boltz2", "openfold2"):
                    if model not in args.cases:
                        continue
                    payload = customer.original_fixture() if model == "phenoage" else {}
                    operation_name = advertised_operation(discovery, model)
                    body = shared.encoded({"operation": operation_name, "payload": payload})
                    case = {
                        "name": model,
                        "model_id": model,
                        "probe": str(uuid4()),
                        "endpoint": f"/v1/models/{model}:invoke",
                        "started_at": shared.now(),
                        "request": body,
                        "request_content_type": "application/json",
                    }
                    response = recorder.request(
                        public,
                        "POST",
                        case["endpoint"],
                        content=body,
                        headers={
                            "content-type": "application/json",
                            "accept-encoding": "identity",
                            "x-api-key": canary,
                            PROBE_HEADER: case["probe"],
                            "Idempotency-Key": "debug-" + case["probe"],
                            "x-fs2-wait-seconds": "0",
                            "x-fs2-deadline-seconds": str(args.timeout_seconds),
                        },
                        expected=(200, 202),
                    )
                    case.update(
                        operation_id=response.headers["x-fs2-operation-id"],
                        response=response.content,
                        status=response.status_code,
                        response_content_type=response.headers.get("content-type"),
                    )
                    row = {
                        "name": model,
                        "model_id": model,
                        "app_id": app_ids[model],
                        "operation_id": case["operation_id"],
                        "advertised_operation": operation_name,
                    }
                    evidence["cases"].append(row)
                    write(args.output / f"{model}-accepted.json", row, secrets)
                    print(
                        json.dumps({"case": model, "operation_id": case["operation_id"], "state": "accepted"}),
                        flush=True,
                    )
                    operation, states = terminal(public, recorder, case["operation_id"], args.timeout_seconds)
                    check(
                        operation["status"] == ("succeeded" if model == "phenoage" else "failed"),
                        "native_terminal_semantics_changed",
                    )
                    row.update(terminal_status=operation["status"], transitions=states)
                    result = recorder.request(
                        public,
                        "GET",
                        f"/v1/operations/{case['operation_id']}/result",
                        # A failed operation has no successful /result (409).
                        # Its actual upstream 400/422 is checked separately.
                        expected=(200,) if model == "phenoage" else (409,),
                    )
                    if model == "phenoage":
                        shared.validate_result(model, 0, payload, result.json())
                    row["public_result_http_status"] = result.status_code
                    row["public_capture"] = verify_public_capture(
                        admin, recorder, case, saved, app_ids[model], args.capture_timeout_seconds
                    )
                    row["upstream_captures"] = verify_upstream(
                        admin,
                        recorder,
                        case,
                        saved,
                        app_ids[model],
                        payload,
                        args.capture_timeout_seconds,
                        {200} if model == "phenoage" else {400, 422},
                    )
                    row["outcome"] = "passed"
                if "malformed-http" in args.cases:
                    case = {
                        "name": "malformed-http",
                        "model_id": "phenoage",
                        "probe": str(uuid4()),
                        "endpoint": "/v1/models/phenoage:invoke",
                        "started_at": shared.now(),
                        "request": b'{"operation":"predict-age","payload":',
                        "request_content_type": "application/json",
                    }
                    response = recorder.request(
                        public,
                        "POST",
                        case["endpoint"],
                        content=case["request"],
                        headers={
                            "content-type": "application/json",
                            "accept-encoding": "identity",
                            "x-api-key": canary,
                            PROBE_HEADER: case["probe"],
                        },
                        expected=(400, 422),
                    )
                    check("x-fs2-operation-id" not in response.headers, "malformed_http_admitted_work")
                    case.update(
                        response=response.content,
                        status=response.status_code,
                        response_content_type=response.headers.get("content-type"),
                    )
                    evidence["cases"].append(
                        {
                            "name": case["name"],
                            "outcome": "passed",
                            "public_capture": verify_public_capture(
                                admin, recorder, case, saved, app_ids["phenoage"], args.capture_timeout_seconds
                            ),
                        }
                    )
                for case in asyncio.run(mcp_cases(origin, saved, canary)) if "mcp" in args.cases else []:
                    evidence["cases"].append(
                        {
                            "name": case["name"],
                            "outcome": "passed",
                            "public_capture": verify_public_capture(
                                admin,
                                recorder,
                                case,
                                saved,
                                app_ids.get(case["model_id"]),
                                args.capture_timeout_seconds,
                            ),
                        }
                    )
                evidence["outcome"] = "passed"
            finally:
                if signed_in:
                    recorder.request(admin, "DELETE", "/admin/api/v1/session", expected=(204,))
                    evidence["admin_logged_out"] = True
    except Exception as error:
        evidence.update(outcome="failed", error_type=type(error).__name__)
        if isinstance(error, CheckError):
            evidence["error_code"] = str(error)
    finally:
        evidence["completed_at"] = shared.now()
        if recorder:
            write(args.output / "http-events.json", recorder.events, secrets)
        write(args.output / "summary.json", evidence, secrets)
    print(json.dumps({"outcome": evidence["outcome"], "completed_at": evidence["completed_at"]}), flush=True)
    return int(evidence["outcome"] != "passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Use only after explicit release-manager GO.")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--access-bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--capture-timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    check(30 <= args.timeout_seconds <= 900 and 5 <= args.capture_timeout_seconds <= 180, "timeouts_outside_bound")
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "offline-preparation",
                    "phenoage_fixture_sha256": shared.digest(customer.original_fixture()),
                    "cases": [
                        "phenoage-success",
                        "boltz2-upstream-validation",
                        "openfold2-upstream-validation",
                        "malformed-http",
                        "mcp-list_models",
                        "mcp-invoke_model-invalid",
                    ],
                    "logical_native_operations": len(MODELS & set(args.cases)),
                    "selected_cases": args.cases,
                    "client_replays": 0,
                }
            )
        )
        return 0
    check(all((args.key_file, args.access_bundle, args.output, args.release)), "live_paths_and_release_required")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
