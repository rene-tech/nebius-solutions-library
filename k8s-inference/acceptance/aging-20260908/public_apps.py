#!/usr/bin/env python3
"""Bounded public aging App acceptance; run only after root's live GO.

Two independent clients, one per new aging App, submit two original synthetic
fixtures each. Cold demand uses HTTP; the second prediction uses MCP. Exact
replays must share the original operation. Only these Apps' min/max settings
and two task-scoped temporary keys may change. No Kubernetes writes or retries.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models"))

from aging.contracts import AltumAgeRequest, ClinicalRequest  # noqa: E402
from aging.fixtures import clinical_payload, methylation_payload  # noqa: E402

from fs2_serve.auth import TokenService  # noqa: E402
from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result  # noqa: E402
from fs2_serve.user_models import owner_id  # noqa: E402

MODELS = ("phenoage", "altumage")
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
EVENT_LOCK = threading.Lock()


def now():
    return datetime.now(UTC).isoformat()


def encoded(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def check(condition, code):
    if not condition:
        raise AssertionError(code)


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def emit(event, **fields):
    with EVENT_LOCK:
        print(json.dumps({"event": event, "at": now(), **fields}), flush=True)


def fixtures(artifact_root):
    manifest = json.loads((artifact_root / "manifest.json").read_bytes())
    check(
        hashlib.sha256((artifact_root / "manifest.json").read_bytes()).hexdigest()
        == "321f6ceafcd2ade0d88a75ab2d492698774fb0a05b1f6c4b2a0f8ffca1acc3fa",
        "baked_manifest_changed",
    )
    for name in ("cpgs.json", "preprocessing.npz"):
        check(
            hashlib.sha256((artifact_root / name).read_bytes()).hexdigest() == manifest["artifacts"][name]["sha256"],
            "fixture_artifact_changed",
        )
    batches = {
        "phenoage": clinical_payload(2),
        "altumage": methylation_payload(artifact_root, 2),
    }
    result = {}
    for model, batch in batches.items():
        contract = json.loads((ROOT / "catalog/runtime/native" / f"{model}.json").read_bytes())
        requests = [{**batch, "samples": [sample]} for sample in batch["samples"]]
        check(
            [digest(item) for item in requests]
            == [item["payload_sha256"] for item in contract["semantic_requests"]["requests"]],
            "original_request_hash_changed",
        )
        for request in requests:
            (ClinicalRequest if model == "phenoage" else AltumAgeRequest).model_validate(request)
        result[model] = requests
    return result


def validate_result(model, index, request, result):
    check(
        result["model_id"] == model and result["sample_count"] == 1,
        "response_model_or_batch",
    )
    check(
        result["device"] == ("cpu" if model == "phenoage" else "cuda"),
        "response_device",
    )
    check(len(result["predictions"]) == 1, "response_prediction_count")
    prediction = result["predictions"][0]
    check(
        prediction["sample_id"] == request["samples"][0]["sample_id"],
        "response_sample_id",
    )
    field = "phenotypic_age_years" if model == "phenoage" else "predicted_chronological_age_years"
    value = prediction[field]
    check(isinstance(value, int | float) and math.isfinite(value), "response_nonfinite")
    retained = json.loads((Path(__file__).parent / f"{model}-r01.json").read_bytes())
    expected = retained["native_http_predictions"][index]["body"]["predictions"][0][field]
    tolerance = 1e-10 if model == "phenoage" else 0.001
    check(
        math.isclose(value, expected, rel_tol=0, abs_tol=tolerance),
        "retained_prediction_parity",
    )
    if model == "phenoage":
        check(
            result["model_version"] == "levine-2018-supplement-rounded-v1",
            "formula_identity",
        )
        check(result["gpu_snapshot"] == "not-applicable-cpu", "formula_snapshot_claim")
    else:
        check(
            result["weights_sha256"] == "648f9d8cf8fb809e0ce9f1d46b652a936b2bb056b6d028c369ea5e2ed4a05e87",
            "weights_identity",
        )
        cpu_expected = retained["cpu_cuda_parity"][1]["cpu"][index][field]
        check(abs(value - cpu_expected) <= tolerance, "retained_cpu_parity")
        check(result["gpu_snapshot"] == "not-qualified", "unproven_snapshot_claim")
    return {
        "value": value,
        "expected": expected,
        "absolute_error": abs(value - expected),
        "tolerance": tolerance,
    }


def zero_worker_spec(spec):
    result = copy.deepcopy(spec)
    availability = result["availability"]
    check(not availability["warmWindows"], "warm_windows_require_coordination")
    check(result["lifecycle"]["desiredState"] == "Enabled", "new_app_not_enabled")
    availability["minReplicas"], availability["maxReplicas"] = 0, 1
    return result


class Trace:
    def __init__(self, output):
        self.output, self.ordinal, self.lock = output, 0, threading.Lock()

    def record(self, kind, value):
        with self.lock:
            self.ordinal += 1
            write(self.output / f"{self.ordinal:05d}-{kind}.json", value)


def exchange(
    client,
    trace,
    method,
    path,
    *,
    payload=None,
    expected=(200,),
    headers=None,
    disclosure=False,
):
    started_at, started = now(), time.monotonic()
    row = {
        "started_at": started_at,
        "method": method,
        "path": path,
        "request_sha256": None if payload is None else digest(payload),
    }
    try:
        response = client.request(method, path, json=payload, headers=headers)
        row.update(
            status=response.status_code,
            response_content_type=response.headers.get("content-type"),
            response_bytes=len(response.content),
            response_sha256=hashlib.sha256(response.content).hexdigest(),
            operation_id=response.headers.get("x-fs2-operation-id"),
            replay=response.headers.get("x-fs2-idempotent-replay"),
        )
        try:
            value = response.json() if response.content else None
        except ValueError:
            # A proxy/server's plain-text error is evidence, not an absent
            # HTTP exchange. Never retain a potentially malformed key secret.
            row["non_json_body"] = None if disclosure else response.text[:65536]
            row["non_json_body_truncated"] = not disclosure and len(response.text) > 65536
            raise
        recorded = copy.deepcopy(value)
        if disclosure and isinstance(recorded, dict):
            recorded.get("data", {}).pop("secret", None)
        row["response"] = recorded
    except Exception as error:
        row["error_type"] = type(error).__name__
        raise
    finally:
        row.update(completed_at=now(), duration_seconds=time.monotonic() - started)
        trace.record("http", row)
    check(response.status_code in expected, f"http_{response.status_code}")
    return value, response.headers


def admin_call(admin, trace, method, path, **kwargs):
    value, _ = exchange(admin, trace, method, path, **kwargs)
    return None if value is None else value["data"]


async def mcp_call(origin, token, trace, name, arguments):
    started_at, started = now(), time.monotonic()
    row = {
        "started_at": started_at,
        "tool": name,
        "arguments_sha256": digest(arguments),
    }
    try:
        async with httpx2.AsyncClient(
            timeout=60,
            trust_env=False,
            headers={"authorization": "Bearer " + token, "origin": origin},
        ) as transport:
            async with Client(
                streamable_http_client(origin + "/mcp", http_client=transport),
                mode=MCP_PROTOCOL_VERSION,
            ) as client:
                raw = await client.call_tool(name, arguments)
                row["response"] = raw.model_dump(mode="json", by_alias=True)
                return _mcp_result(raw)
    except Exception as error:
        row["error_type"] = type(error).__name__
        raise
    finally:
        row.update(completed_at=now(), duration_seconds=time.monotonic() - started)
        trace.record("mcp", row)


def app_observation(admin, trace, app_path):
    containers = admin_call(admin, trace, "GET", app_path + "/containers")
    summary = admin_call(admin, trace, "GET", app_path)
    check(
        containers["state"] == "available" and not containers["truncated"],
        "containers_unavailable",
    )
    return {"at": now(), "summary": summary, "containers": containers}


def wait_zero(admin, trace, app_path, deadline, phase):
    consecutive = 0
    while time.monotonic() < deadline:
        observation = app_observation(admin, trace, app_path)
        cold = observation["containers"]["total"] == 0 and observation["summary"]["status"] == "Cold"
        consecutive = consecutive + 1 if cold else 0
        if consecutive == 2:
            emit("zero_workers", phase=phase, app_id=observation["summary"]["app_id"])
            return observation
        time.sleep(5 if phase == "before" else 15)
    raise AssertionError("zero_workers_timeout_" + phase)


def await_terminal(public, admin, trace, app_path, operation_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    observations, transitions, previous = [], [], None
    while time.monotonic() < deadline:
        operation, _ = exchange(public, trace, "GET", f"/v1/operations/{operation_id}")
        observation = app_observation(admin, trace, app_path)
        observations.append(observation)
        if operation["status"] != previous:
            transitions.append({"at": now(), "status": operation["status"]})
            previous = operation["status"]
        if operation["status"] in TERMINAL:
            check(operation["status"] == "succeeded", "operation_" + operation["status"])
            return operation, observations, transitions
        time.sleep(5)
    raise AssertionError("operation_poll_deadline")


def accepted(evidence, request, operation_id, index):
    # Preserve admission before testing replay: a failed replay must not hide
    # a real, already-durable operation or its subsequent resource usage.
    evidence["operations"].append(
        {
            "operation_id": operation_id,
            "request_sha256": digest(request),
            "submission_index": index,
        }
    )
    emit(
        "accepted",
        model_id=evidence["model_id"],
        app_id=evidence["app_id"],
        operation_id=operation_id,
        index=index,
    )


def run_model(args, model, requests, origin, admin, admin_trace, source_key, app):
    output = args.output / model
    output.mkdir(mode=0o700)
    trace = Trace(output)
    app_path = f"/admin/api/v1/apps/{app['app_id']}"
    run_id = f"aging-public-20260908-{args.output.name}-{model}"
    evidence = {
        "model_id": model,
        "app_id": app["app_id"],
        "release": args.release,
        "started_at": now(),
        "outcome": "running",
        "operations": [],
    }
    key_id = token = None
    window = "?" + urlencode({"from": now(), "limit": 100})
    try:
        before = admin_call(admin, admin_trace, "GET", app_path + "/settings")
        evidence["original_settings"] = before
        check(
            before["serving"] is not None and before["capabilities"]["live_settings"],
            "managed_app_settings_unavailable",
        )
        # These native Apps belong to the normal inference tenant, not the
        # separate academic scientific tenant. Check before any test mutation.
        check(before["serving"]["tenant_id"] == source_key["tenant_id"], "source_key_tenant_mismatch")
        spec = zero_worker_spec(before["serving"]["spec"])
        settings_started_at, settings_started = now(), time.monotonic()
        changed = before
        if spec != before["serving"]["spec"]:
            changed = admin_call(
                admin,
                admin_trace,
                "PATCH",
                app_path + "/settings",
                payload={
                    "expected_app_revision": before["app_revision"],
                    "serving_base_etag": before["serving"]["etag"],
                    "serving_spec": spec,
                },
            )
        evidence["test_settings"] = changed
        check(changed["serving"]["spec"] == spec, "live_settings_differ")
        limits = spec["availability"]
        timeout = limits.get("startupTimeoutSeconds") or 900
        quiet_timeout = limits["idleSeconds"] + limits["cooldownSeconds"] + 120
        evidence["zero_before"] = wait_zero(admin, admin_trace, app_path, time.monotonic() + quiet_timeout, "before")
        evidence["settings_to_cold"] = {
            "started_at": settings_started_at,
            "observed_at": evidence["zero_before"]["at"],
            "client_seconds": time.monotonic() - settings_started,
            "settings_changed": spec != before["serving"]["spec"],
        }
        owner = owner_id(source_key["tenant_id"], source_key["principal_id"])
        disclosure = admin_call(
            admin,
            admin_trace,
            "POST",
            f"/admin/api/v1/users/{owner}/keys",
            expected=(201,),
            disclosure=True,
            payload={
                "name": run_id,
                "principal_id": source_key["principal_id"],
                "tenant_id": source_key["tenant_id"],
                "models": [model],
                "scopes": source_key["scopes"],
                "max_concurrency": 1,
                "request_budget": 4,
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            },
        )
        key_id, token = disclosure["key"]["id"], disclosure["secret"]
        evidence["key_id"] = key_id
        with httpx.Client(
            base_url=origin,
            timeout=60,
            trust_env=False,
            headers={"authorization": "Bearer " + token, "origin": origin},
        ) as public:
            discovery, _ = exchange(public, trace, "GET", "/v1/models")
            check(
                [item["id"] for item in discovery["data"]] == [model],
                "http_model_scope",
            )
            mcp_models = asyncio.run(mcp_call(origin, token, trace, "list_models", {}))
            check(
                [item["id"] for item in mcp_models["data"]] == [model],
                "mcp_model_scope",
            )
            for index, request in enumerate(requests):
                idem = f"{run_id}-{index}"
                started = time.monotonic()
                if index == 0:
                    payload = {"operation": "predict-age", "payload": request}
                    _, headers = exchange(
                        public,
                        trace,
                        "POST",
                        f"/v1/models/{model}:invoke",
                        payload=payload,
                        expected=(200, 202),
                        headers={
                            "Idempotency-Key": idem,
                            "x-fs2-wait-seconds": "0",
                            "x-fs2-deadline-seconds": str(timeout + 120),
                        },
                    )
                    operation_id = headers["x-fs2-operation-id"]
                    accepted(evidence, request, operation_id, index)
                    _, replay_headers = exchange(
                        public,
                        trace,
                        "POST",
                        f"/v1/models/{model}:invoke",
                        payload=payload,
                        expected=(200, 202),
                        headers={"Idempotency-Key": idem, "x-fs2-wait-seconds": "0"},
                    )
                    check(
                        replay_headers["x-fs2-operation-id"] == operation_id
                        and replay_headers["x-fs2-idempotent-replay"] == "true",
                        "http_replay_identity",
                    )
                else:
                    parameters = {
                        "model_id": model,
                        "protocol": "native",
                        "payload": request,
                        "idempotency_key": idem,
                        "wait_seconds": 0,
                    }
                    operation = asyncio.run(mcp_call(origin, token, trace, "invoke_model", parameters))
                    operation_id = operation["id"]
                    accepted(evidence, request, operation_id, index)
                    replay = asyncio.run(mcp_call(origin, token, trace, "invoke_model", parameters))
                    check(
                        replay["id"] == operation_id and replay["reused"],
                        "mcp_replay_identity",
                    )
                terminal, observations, transitions = await_terminal(
                    public,
                    admin,
                    trace,
                    app_path,
                    operation_id,
                    timeout + 120,
                )
                result, _ = exchange(public, trace, "GET", f"/v1/operations/{operation_id}/result")
                validation = validate_result(model, index, request, result)
                mcp_result = asyncio.run(
                    mcp_call(
                        origin,
                        token,
                        trace,
                        "get_operation_result",
                        {"operation_id": operation_id},
                    )
                )
                check(
                    mcp_result["operation"]["id"] == operation_id and mcp_result["result"] == result,
                    "mcp_result_identity",
                )
                check(
                    any(
                        container["ready"] for observed in observations for container in observed["containers"]["items"]
                    ),
                    "no_ready_worker_observed",
                )
                evidence["operations"][-1].update(
                    terminal=terminal,
                    result=result,
                    validation=validation,
                    transitions=transitions,
                    app_observations=observations,
                    client_seconds=time.monotonic() - started,
                    accepted_to_terminal_seconds=(
                        datetime.fromisoformat(terminal["completed_at"])
                        - datetime.fromisoformat(terminal["accepted_at"])
                    ).total_seconds(),
                )
                emit(
                    "completed",
                    model_id=model,
                    app_id=app["app_id"],
                    operation_id=operation_id,
                    client_seconds=evidence["operations"][-1]["client_seconds"],
                )
            check(
                evidence["operations"][0]["validation"]["value"] != evidence["operations"][1]["validation"]["value"],
                "responses_not_distinct",
            )
            evidence["zero_after"] = wait_zero(admin, admin_trace, app_path, time.monotonic() + quiet_timeout, "after")
            runs = admin_call(admin, admin_trace, "GET", app_path + "/runs" + window)
            ids = {item["operation"]["id"] for item in runs["items"]}
            check(
                ids == {item["operation_id"] for item in evidence["operations"]},
                "logical_runs_not_two",
            )
            evidence["runs"] = runs
            evidence["usage"] = admin_call(admin, admin_trace, "GET", app_path + "/usage" + window)
            check(evidence["usage"]["logical_runs"] == 2, "logical_usage_not_two")
            evidence["logs"] = admin_call(admin, admin_trace, "GET", app_path + "/logs" + window)
            final = admin_call(admin, admin_trace, "GET", app_path + "/settings")
            check(final["serving"]["spec"] == spec, "test_settings_changed")
            evidence["final_settings"] = final
            evidence["outcome"] = "passed"
    except Exception as error:
        evidence.update(outcome="failed", error_type=type(error).__name__)
        if isinstance(error, AssertionError):
            evidence["error_code"] = str(error)
        emit(
            "failed",
            model_id=model,
            error_type=type(error).__name__,
            error_code=evidence.get("error_code"),
        )
    finally:
        if key_id is not None:
            try:
                revoked_key = admin_call(admin, admin_trace, "DELETE", f"/admin/api/v1/keys/{key_id}")
                check(revoked_key["state"] == "revoked", "key_revocation_unconfirmed")
                with httpx.Client(
                    base_url=origin,
                    timeout=30,
                    trust_env=False,
                    headers={"authorization": "Bearer " + token},
                ) as revoked:
                    exchange(revoked, trace, "GET", "/v1/models", expected=(401,))
                evidence["test_key_revoked"] = True
            except Exception as error:
                evidence.update(outcome="failed", key_cleanup_error=type(error).__name__)
        evidence["completed_at"] = now()
        write(output / "outcome.json", evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    requests = fixtures(args.artifact_root)
    plan = {
        "release": args.release,
        "logical_operations": 4,
        "parallel_clients": 2,
        "maximum_new_gpu_workers": 1,
        "models": {model: [digest(request) for request in items] for model, items in requests.items()},
        "public_cold_boundary": "zero reusable workers; actual node provisioning and image/cache state must be reported from observation, not assumed",
        "native_worker_qualification": "retained direct receipts; public acceptance does not infer GPU snapshots",
    }
    if args.prepare_only:
        print(json.dumps({**plan, "mutations_performed": False}, sort_keys=True))
        return 0
    check(
        args.output is not None and args.access_bundle is not None,
        "output_and_access_required",
    )
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write(args.output / "plan.json", plan)
    trace = Trace(args.output)
    access = json.loads(args.access_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    token_id, _ = TokenService._parse(access["credentials"]["inference_access_token"])
    result = {"started_at": now(), "release": args.release, "outcome": "failed"}
    with httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin:
        try:
            exchange(
                admin,
                trace,
                "POST",
                "/admin/api/v1/session",
                headers={
                    "authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"],
                },
            )
            all_apps = admin_call(admin, trace, "GET", "/admin/api/v1/apps")["items"]
            apps = {model: next(item for item in all_apps if item["public_model_id"] == model) for model in MODELS}
            for model, app in apps.items():
                check(
                    app["model_ref"] == model and app["execution_mode"] == "serving",
                    "canonical_app_identity",
                )
            keys = admin_call(admin, trace, "GET", "/admin/api/v1/keys?limit=1000")["items"]
            source_key = next(item for item in keys if item["id"] == str(token_id))
            emit(
                "campaign_started",
                release=args.release,
                apps={model: app["app_id"] for model, app in apps.items()},
            )
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [
                    workers.submit(
                        run_model,
                        args,
                        model,
                        requests[model],
                        origin,
                        admin,
                        trace,
                        source_key,
                        apps[model],
                    )
                    for model in MODELS
                ]
                outcomes = [future.result() for future in futures]
            result["models"] = [
                {
                    "model_id": item["model_id"],
                    "app_id": item["app_id"],
                    "outcome": item["outcome"],
                    "operation_ids": [operation["operation_id"] for operation in item["operations"]],
                }
                for item in outcomes
            ]
            result["outcome"] = "passed" if all(item["outcome"] == "passed" for item in outcomes) else "failed"
        except Exception as error:
            result["error_type"] = type(error).__name__
        finally:
            try:
                exchange(admin, trace, "DELETE", "/admin/api/v1/session", expected=(204,))
            except Exception as error:
                result.update(outcome="failed", logout_error=type(error).__name__)
            result["completed_at"] = now()
            write(args.output / "outcome.json", result)
            emit("campaign_completed", **result)
    return 0 if result["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
