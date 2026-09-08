#!/usr/bin/env python3
"""One real scientific app clone over an unchanged accepted Protenix fixture.

Run only after the release owner's explicit deployment-ready signal. This uses
the existing public scientific upload/result validators, one logical operation,
exact HTTP/MCP replays, and public admin APIs. It never changes a source app.
Credentials stay in memory; complete response evidence is private.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/scientific-fleet"))
import run_acceptance as public  # noqa: E402
import run_fleet_acceptance as fleet  # noqa: E402
import run_scenario_acceptance as scenario  # noqa: E402

from fs2_serve.auth import TokenService  # noqa: E402
from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result  # noqa: E402
from fs2_serve.user_models import owner_id  # noqa: E402


def now():
    return datetime.now(UTC).isoformat()


def check(condition, code):
    if not condition:
        raise public.AcceptanceError(code)


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def settings_identity(value):
    """Compare desired settings, not changing admission/runtime observations."""
    return {
        **{
            key: value[key]
            for key in ("app_revision", "display_name", "academic_required")
        },
        "scientific_desired": value["scientific"]["desired"],
    }


def check_run_increment(prior_ids, current_runs, operation_id):
    current_ids = [item["operation"]["id"] for item in current_runs]
    check(
        operation_id not in prior_ids
        and len(current_ids) == len(prior_ids) + 1
        and set(current_ids) == {*prior_ids, operation_id},
        "clone_logical_run_count",
    )


def fixture():
    selected = next(
        item for item in fleet.discover_inputs(ROOT) if item.model_id == "protenix-v2"
    )
    config = public.RunConfig(
        endpoint="https://example.invalid",
        repository_root=ROOT,
        activation_fragment=selected.path,
        receipt_path=Path("unused"),
        run_id="apps-science-prepare",
    )
    model_id, request, declarations, fragment = public._activation(config)
    plan = {
        "source_model_id": model_id,
        "fixture": selected.relative_path,
        "fixture_sha256": selected.sha256,
        "request_sha256": hashlib.sha256(public._canonical_json(request)).hexdigest(),
        "parameters": request["parameters"],
        "service_class": request["service_class"],
        "inputs": [
            {
                "role": item.role,
                "name": item.name,
                "sha256": hashlib.sha256(item.data).hexdigest(),
                "size_bytes": len(item.data),
            }
            for item in declarations
        ],
        "logical_operations": 1,
        "parallel_clients": 1,
    }
    return plan, request, declarations, fragment


class RecordedPublic(public.PublicApiClient):
    def __init__(self, endpoint, token, output):
        super().__init__(endpoint, token)
        self.output = output

    def request(self, method, path, **kwargs):
        started_at, started = now(), time.monotonic()
        response = super().request(method, path, **kwargs)
        record = {
            "started_at": started_at,
            "completed_at": now(),
            "method": method,
            "path": path,
            "status": response.status,
            "elapsed_seconds": time.monotonic() - started,
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
        }
        if "json" in response.headers.get("content-type", ""):
            record["response"] = json.loads(response.body)
        with (self.output / "public-http.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        return response


async def mcp_checks(
    origin, token, clone, output, *, operation_id=None, request=None, run_id=None
):
    tool_name = "app_" + clone["app_id"].replace("-", "")
    evidence = {"started_at": now(), "app_id": clone["app_id"], "tool_name": tool_name}
    async with httpx2.AsyncClient(
        timeout=60,
        trust_env=False,
        follow_redirects=False,
        headers={"authorization": "Bearer " + token, "origin": origin},
    ) as transport:
        async with Client(
            streamable_http_client(origin + "/mcp", http_client=transport),
            mode=MCP_PROTOCOL_VERSION,
        ) as client:
            tools = await client.list_tools()
            evidence["tools"] = [item.name for item in tools.tools]
            check(tool_name in evidence["tools"], "clone_mcp_tool_missing")
            models = _mcp_result(await client.call_tool("list_scientific_models", {}))
            evidence["models"] = models
            check(
                clone["public_model_id"]
                in {item["model_id"] for item in models["data"]},
                "clone_mcp_model_missing",
            )
            if operation_id is not None:
                request_digest = hashlib.sha256(
                    public._canonical_json(request)
                ).hexdigest()
                replay = _mcp_result(
                    await client.call_tool(
                        tool_name,
                        {
                            "request": request,
                            "idempotency_key": public._idempotency_key(
                                run_id, "submit", request_digest
                            ),
                        },
                    )
                )
                evidence["named_tool_replay"] = replay
                check(
                    replay["operation"]["id"] == operation_id
                    and replay["operation"]["reused"],
                    "mcp_replay_identity",
                )
                evidence["status"] = _mcp_result(
                    await client.call_tool(
                        "get_scientific_status", {"operation_id": operation_id}
                    )
                )
                evidence["result"] = _mcp_result(
                    await client.call_tool(
                        "get_scientific_result", {"operation_id": operation_id}
                    )
                )
                check(
                    evidence["status"]["operation"]["status"] == "succeeded",
                    "mcp_terminal_status",
                )
                check(
                    evidence["result"]["semantic_validation"]["status"] == "passed",
                    "mcp_semantic_result",
                )
    evidence["completed_at"] = now()
    write(
        output / ("mcp-terminal.json" if operation_id else "mcp-discovery.json"),
        evidence,
    )


def run(args):
    os.umask(0o077)
    plan, request, declarations, fragment = fixture()
    if args.prepare_only:
        print(
            json.dumps(
                {**plan, "release": args.release, "mutations_performed": False},
                sort_keys=True,
            )
        )
        return 0
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write(args.output / "fixture-plan.json", plan)
    access = json.loads(args.access_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    token = access["credentials"]["scientific_access_token"]
    evidence = {
        "release": args.release,
        "started_at": now(),
        "outcome": "running",
        "logical_operations": 1,
    }
    clone = issued_key = operation_id = source = source_settings = None
    terminal = None
    admin_ordinal = 0
    run_id = "apps-scientific-20260908-" + args.output.name
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, timeout=60, trust_env=False
    ) as admin:

        def call(method, path, *, payload=None, expected=200, disclosure=False):
            nonlocal admin_ordinal
            started_at, started = now(), time.monotonic()
            response = admin.request(method, path, json=payload)
            admin_ordinal += 1
            value = response.json()
            recorded = json.loads(response.content)
            if disclosure and response.status_code == expected:
                recorded["data"].pop("secret", None)
            write(
                args.output / f"admin-{admin_ordinal:03d}.json",
                {
                    "started_at": started_at,
                    "completed_at": now(),
                    "method": method,
                    "path": path,
                    "status": response.status_code,
                    "elapsed_seconds": time.monotonic() - started,
                    "response": recorded,
                },
            )
            check(
                response.status_code == expected, f"admin_http_{response.status_code}"
            )
            return value["data"]

        login = admin.post(
            "/admin/api/v1/session",
            headers={
                "authorization": "Bearer "
                + access["credentials"]["admin_bootstrap_token"],
            },
        )
        evidence["login_status"] = login.status_code
        try:
            check(login.status_code == 200, "admin_login_failed")
            apps = call("GET", "/admin/api/v1/apps")["items"]
            source = next(
                item for item in apps if item["public_model_id"] == "protenix-v2"
            )
            source_path = f"/admin/api/v1/apps/{source['app_id']}"
            source_settings = call("GET", source_path + "/settings")
            evidence["source_app_id"] = source["app_id"]
            if args.existing_app_id:
                clone = call("GET", f"/admin/api/v1/apps/{args.existing_app_id}")
                check(
                    clone["app_id"] != source["app_id"]
                    and clone["model_ref"] == source["model_ref"]
                    and clone["display_name"].startswith("apps-scientific-20260908-")
                    and clone["execution_mode"] == "scientific",
                    "existing_app_is_not_owned_scientific_test_clone",
                )
            else:
                clone = call(
                    "POST",
                    "/admin/api/v1/apps",
                    payload={
                        "model_ref": source["model_ref"],
                        "source_app_id": source["app_id"],
                        "display_name": run_id,
                    },
                    expected=201,
                )
            evidence.update(
                app_id=clone["app_id"], public_model_id=clone["public_model_id"]
            )
            print(
                json.dumps(
                    {
                        "event": "clone_resumed"
                        if args.existing_app_id
                        else "clone_created",
                        "at": now(),
                        **{key: evidence[key] for key in ("app_id", "public_model_id")},
                    }
                ),
                flush=True,
            )
            clone_path = f"/admin/api/v1/apps/{clone['app_id']}"
            clone_settings = call("GET", clone_path + "/settings")
            # Retained failed acceptance operations are real history. Compare
            # against a fixed lower time boundary, never delete/reclassify them
            # or assume a resumed owned app has an empty history.
            window = "?" + urlencode({"from": (datetime.now(UTC) - timedelta(days=1)).isoformat()})
            prior_runs = call("GET", clone_path + "/runs" + window)["items"]
            prior_ids = [item["operation"]["id"] for item in prior_runs]
            prior_usage_count = call("GET", clone_path + "/usage" + window)["logical_runs"]
            evidence["prior_operation_ids"] = prior_ids
            evidence["prior_logical_runs"] = prior_usage_count
            check(
                clone_settings["scientific"]["desired"]["startup_policies"]
                == source_settings["scientific"]["desired"]["startup_policies"],
                "clone_startup_policy_changed",
            )

            original_key_id, _ = TokenService._parse(token)
            keys = call("GET", "/admin/api/v1/keys?limit=1000")["items"]
            original_key = next(
                item for item in keys if item["id"] == str(original_key_id)
            )
            if (
                "*" not in original_key["models"]
                and clone["public_model_id"] not in original_key["models"]
            ):
                owner = owner_id(
                    original_key["tenant_id"], original_key["principal_id"]
                )
                disclosure = call(
                    "POST",
                    f"/admin/api/v1/users/{owner}/keys",
                    expected=201,
                    disclosure=True,
                    payload={
                        "name": run_id,
                        "principal_id": original_key["principal_id"],
                        "tenant_id": original_key["tenant_id"],
                        "models": [clone["public_model_id"]],
                        "scopes": original_key["scopes"],
                        "max_concurrency": 1,
                        "request_budget": 4,
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=2)
                        ).isoformat(),
                    },
                )
                issued_key = disclosure["key"]["id"]
                token = disclosure["secret"]
                evidence["test_key_id"] = issued_key

            client = RecordedPublic(origin, token, args.output)
            discovery = public._json_response(
                client.request("GET", "/v1/scientific-models"), 200, "discovery"
            )
            check(
                clone["public_model_id"]
                in {item["model_id"] for item in discovery["data"]},
                "clone_http_model_missing",
            )
            asyncio.run(mcp_checks(origin, token, clone, args.output))
            request, uploads = public._prepare_input(
                client,
                model_id=clone["public_model_id"],
                request=request,
                declarations=declarations,
                run_id=run_id,
            )
            operation_id, initial = public._submit(
                client,
                model_id=clone["public_model_id"],
                request=request,
                run_id=run_id,
            )
            evidence["operation_id"] = operation_id
            print(
                json.dumps(
                    {
                        "event": "submitted",
                        "at": now(),
                        "operation_id": operation_id,
                        "app_id": clone["app_id"],
                    }
                ),
                flush=True,
            )
            write(
                args.output / "submitted.json",
                {
                    "operation_id": operation_id,
                    "app_id": clone["app_id"],
                    "accepted": initial,
                },
            )
            replay_id, replay = public._submit(
                client,
                model_id=clone["public_model_id"],
                request=request,
                run_id=run_id,
            )
            check(
                replay_id == operation_id and replay["operation"]["reused"],
                "http_replay_identity",
            )
            terminal = public._poll(
                client,
                operation_id=operation_id,
                model_id=clone["public_model_id"],
                operation=request["operation"],
                initial=initial,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=5,
            )
            result = public._json_response(
                client.request("GET", f"/v1/operations/{operation_id}/result"),
                200,
                "result",
            )
            public._validate_result(
                result,
                status=terminal,
                operation_id=operation_id,
                model_id=clone["public_model_id"],
                input_pointer=request["input_manifest"],
                fragment=fragment,
            )
            evidence["downloads"] = scenario.verify_result_downloads(client, result)
            receipt = public._receipt(
                client=client,
                model_id=clone["public_model_id"],
                status=terminal,
                result=result,
                uploads=uploads,
            )
            write(args.output / "scientific-receipt.json", receipt)
            check(
                all(
                    attempt["resource_released"]
                    for stage in terminal["batch"]["stages"]
                    for attempt in stage["attempts"]
                ),
                "resource_release_missing",
            )
            asyncio.run(
                mcp_checks(
                    origin,
                    token,
                    clone,
                    args.output,
                    operation_id=operation_id,
                    request=request,
                    run_id=run_id,
                )
            )
            clone_runs = call("GET", clone_path + "/runs" + window)["items"]
            check_run_increment(prior_ids, clone_runs, operation_id)
            detail = call("GET", clone_path + f"/runs/{operation_id}")
            check(
                detail["scientific"] is not None and detail["observed_transport"],
                "clone_run_enrichment_missing",
            )
            source_runs = call("GET", source_path + "/runs")["items"]
            check(
                operation_id not in {item["operation"]["id"] for item in source_runs},
                "source_history_contaminated",
            )
            check(
                call("GET", clone_path + "/usage" + window)["logical_runs"] == prior_usage_count + 1,
                "clone_usage_double_count",
            )
            evidence.update(
                outcome="passed",
                http_replay=True,
                mcp_replay=True,
                resources_released=True,
            )
        except Exception as error:
            evidence.update(
                outcome="failed",
                error_code=getattr(error, "code", type(error).__name__),
            )
        finally:
            # _poll raises for a real terminal failure before assigning its
            # return value. Recover that read-only final receipt so clone-only
            # policy cleanup still runs for failed, released operations.
            try:
                if clone is not None and operation_id is not None and terminal is None:
                    latest = public._json_response(
                        client.request("GET", f"/v1/operations/{operation_id}"),
                        200, "cleanup_status",
                    )
                    if latest["operation"]["status"] in {"succeeded", "failed", "cancelled"}:
                        terminal = latest
                        evidence["terminal_cleanup_status"] = latest["operation"]["status"]
                        evidence["terminal_cleanup_resources_released"] = all(
                            attempt["resource_released"]
                            for stage in latest["batch"]["stages"]
                            for attempt in stage["attempts"]
                        )
                if clone is not None and (operation_id is None or terminal is not None):
                    clone_path = f"/admin/api/v1/apps/{clone['app_id']}"
                    current = call("GET", clone_path + "/settings")
                    desired = current["scientific"]["desired"]
                    paused = call(
                        "PATCH",
                        clone_path + "/settings",
                        payload={
                            "expected_app_revision": current["app_revision"],
                            "scientific_policy": {
                                "expected_revision": desired["revision"],
                                "paused": True,
                                "max_active_runs": desired["max_active_runs"],
                                "startup_policies": desired["startup_policies"],
                                "reason": "Completed bounded independent app acceptance",
                            },
                        },
                    )
                    evidence["clone_paused"] = paused["scientific"]["desired"]["paused"]
                if source is not None and source_settings is not None:
                    unchanged = call(
                        "GET", f"/admin/api/v1/apps/{source['app_id']}/settings"
                    )
                    evidence["source_settings_unchanged"] = settings_identity(
                        unchanged
                    ) == settings_identity(source_settings)
                    check(
                        evidence["source_settings_unchanged"], "source_settings_changed"
                    )
            except Exception as error:
                evidence.update(
                    outcome="failed",
                    cleanup_error=getattr(error, "code", type(error).__name__),
                )
            try:
                if issued_key is not None:
                    revoked = call("DELETE", f"/admin/api/v1/keys/{issued_key}")
                    check(revoked["state"] == "revoked", "key_revocation_unconfirmed")
                    response = public.PublicApiClient(origin, token).request(
                        "GET", "/v1/scientific-models"
                    )
                    evidence["revoked_key_http_status"] = response.status
                    check(response.status == 401, "revoked_key_accepted")
            except Exception as error:
                evidence.update(
                    outcome="failed",
                    key_cleanup_error=getattr(error, "code", type(error).__name__),
                )
            evidence["logout_status"] = admin.delete(
                "/admin/api/v1/session"
            ).status_code
    evidence["completed_at"] = now()
    write(args.output / "outcome.json", evidence)
    print(json.dumps(evidence, sort_keys=True), flush=True)
    return 0 if evidence["outcome"] == "passed" else 1


if __name__ == "__main__":
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument(
        "--existing-app-id",
        help="Explicit owned clone from a prior pre-submission failure",
    )
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--prepare-only", action="store_true")
    raise SystemExit(run(parser.parse_args()))
